#!/usr/bin/env python3
"""CLI for image generation and editing through the Acctoken API relay.

Calls an OpenAI-compatible images endpoint (default https://www.acctoken.com/v1) and
picks a model cost-first by default: `--model auto` resolves to the cheapest model the
API key can actually reach that fits the task (see scripts/model_catalog.py and the
`models` subcommand). Pin a model id to bypass the resolver. Supports generate, edit,
generate-batch, and models.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

try:  # Image model + price catalog (cost-aware selection). Bundled with the skill.
    import model_catalog as _catalog
except ImportError:  # pragma: no cover - stays functional without the catalog
    _catalog = None  # type: ignore[assignment]

# "auto" defers to the cost-aware resolver in model_catalog: the cheapest model
# that can satisfy the request, checked against the models the API key can
# actually reach (GET /v1/models). Pin a model id to bypass the resolver.
DEFAULT_MODEL = "auto"
DEFAULT_PROFILE = "cheapest"
DEFAULT_TASK = "auto"
DEFAULT_BASE_URL = "https://www.acctoken.com/v1"
DEFAULT_QUALITY = "medium"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 5
DEFAULT_DOWNSCALE_SUFFIX = "-web"
DEFAULT_OUTPUT_PATH = "output/imagegen/output.png"
GPT_IMAGE_MODEL_PREFIX = "gpt-image-"

# Thinking knob (gemini-nano-banana-2.1). The relay bills thinking tokens as output,
# so `minimal` is the cost-preserving default and `high` is opt-in. THINKING_TOKENS is
# the observed thinking-token count per level, used only to make the cost estimate on
# the high setting honest: measured 0 tokens at minimal and ~820 at high (1K output).
DEFAULT_THINKING = "minimal"
THINKING_TOKENS = {"minimal": 0, "high": 820}

API_KEY_ENV_VARS = ("ACCTOKEN_API_KEY", "IMAGE_API_KEY", "OPENAI_API_KEY")
KEY_FILE_NAME = "acctoken-imagegen.env"
# Per-agent legacy locations, kept as a fallback so pre-existing installs keep
# working. The skill itself is not bound to any of them: they are searched after
# the agent-neutral locations below.
LEGACY_KEY_FILES = (
    "~/.codex/acctoken-imagegen.env",
    "~/.pi/agent/acctoken-imagegen.env",
    "~/.claude/acctoken-imagegen.env",
)


def _key_file_candidates() -> list[Path]:
    """Key file locations to try, in order. ACCTOKEN_IMAGE_ENV always wins."""
    override = os.getenv("ACCTOKEN_IMAGE_ENV", "").strip()
    if override:
        return [Path(os.path.expanduser(override))]

    config_home = os.getenv("XDG_CONFIG_HOME", "").strip()
    roots = [Path(os.path.expanduser(config_home)) if config_home else Path.home() / ".config", Path.home()]
    candidates: list[Path] = []
    for root in roots:
        candidates.append(root / KEY_FILE_NAME)
        candidates.append(root / "acctoken-imagegen" / "env")
    candidates.extend(Path(os.path.expanduser(p)) for p in LEGACY_KEY_FILES)

    seen = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _key_file_path() -> Path:
    """Preferred location, used for messages and by --save-key."""
    for path in _key_file_candidates():
        if path.is_file():
            return path
    return _key_file_candidates()[0]


def _read_key_from_file() -> str | None:
    for path in _key_file_candidates():
        value = _read_key_from(path)
        if value:
            return value
    return None


def _read_key_from(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    wanted = {"ACCTOKEN_API_KEY", "IMAGE_API_KEY", "OPENAI_API_KEY"}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in wanted:
            value = value.strip().strip("'\"")
            if value:
                return value
    return None


def _is_gpt_image_2_family(model: str) -> bool:
    return model.startswith("gpt-image-2")

ALLOWED_LEGACY_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
KNOWN_MODEL_PREFIXES = (
    "gpt-image-",
    "qwen-image-",
    "dall-e",
    "flux",
    "seedream",
    "gemini-",
    "nano-banana",
    "wan-",
    "z-image",
)
OPEN_MODEL_MIN_EDGE = 256
OPEN_MODEL_MAX_EDGE = 4096
OPEN_MODEL_MAX_RATIO = 4.0
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_INPUT_FIDELITIES = {"low", "high", None}

# Task categories the cost-aware resolver understands. `auto` lets the CLI infer
# the task (edit when an input image is involved, general otherwise).
CATALOG_TASKS = (
    "auto",
    "draft",
    "general",
    "photo",
    "illustration",
    "text",
    "edit",
    "cutout",
    "upscale",
    "layers",
    "4k",
)

# The edits endpoint does not accept `moderation` (the SDK signature rejects it as an
# unexpected keyword argument), so the flag is generate / generate-batch only.
GENERATE_ONLY_OPTIONS = ("moderation",)

# The edits endpoint also rejects `size: "auto"`, and so does the seedream family on
# generation: the relay answers "The parameter `size` specified in the request are not
# valid: size 'auto' is only supported with layer_decomposition". `auto` therefore is
# NOT a safe default. --size uses argparse.SUPPRESS and main() resolves it per endpoint:
# an unspecified generate size falls back to the selected model's own default size, an
# unspecified edit size is omitted from the request entirely, and an explicit
# `--size auto` on a model that does not accept it fails locally with a usable hint
# instead of surfacing a relay 400.
#
# Measured behavior when `size` is omitted on /v1/images/edits (seedream-5.0-flash, 2026-10-11):
# the relay picks the output resolution itself. A 1024x1536 input came back as 1664x2496 --
# the aspect ratio is preserved but the pixel dimensions are NOT the input's. So omitting
# size means "let the relay decide", never "keep the input size". Pin `--size` explicitly
# whenever the asset must land on exact dimensions.
EDIT_REJECTED_SIZES = {"auto"}

GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_MAX_RATIO = 3.0

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500


def _die(message: str, code: int = 1) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _coerce_int(value: Any, field: str, *, label: str = "") -> int:
    """int() with an actionable message instead of a traceback.

    Batch jobs come from user-authored JSONL, so `"n": "oops"` is a data error the
    user can fix, not a stack trace to debug.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        _fail(label, f"{field} must be an integer, got {value!r}.")


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _fail(label: str, message: str) -> NoReturn:
    """Die with an optional prefix, so batch errors name the job that failed."""
    _die(f"{label}: {message}" if label else message)


def _dependency_hint(package: str, *, upgrade: bool = False) -> str:
    command = f"uv pip install {'-U ' if upgrade else ''}{package}"
    return (
        "Re-run through scripts/run.sh, which resolves a working interpreter for you. "
        f"To install into the environment you are running directly: `{command}`. "
        "Point the skill at an existing environment with ACCTOKEN_IMAGEGEN_VENV, or at an "
        "interpreter with ACCTOKEN_IMAGEGEN_PYTHON."
    )


# --------------------------------------------------------------------------- #
# Cost-aware model selection
#
# The resolver answers one question: given the task in front of us, what is the
# cheapest model the *current API key* can actually reach? The candidate price
# catalog lives in scripts/model_catalog.py (generated from the relay's own
# /api/pricing). The set of reachable models comes from GET /v1/models, which is
# authoritative per key -- an unrestricted key sees a dozen image models, a
# restricted key may see five, and picking a model the key cannot use returns a
# permission error without charging, so we probe first and cache the result.
# --------------------------------------------------------------------------- #
DEFAULT_MODELS_TTL = 6 * 60 * 60  # seconds


def _catalog_entry(model: str) -> Any:
    if _catalog is None or not model or model == "auto":
        return None
    return _catalog.CATALOG.get(model)  # type: ignore[union-attr]


def _catalog_excluded_reason(model: str) -> str | None:
    """Why a model id is deliberately absent from the catalog, or None.

    Excluded ids must not reach the "unknown model, pass it through unchanged" path:
    that path is for ids the catalog has never priced, not for ids that were removed
    on purpose (see EXCLUDED_MODELS in scripts/model_catalog.py).
    """
    if _catalog is None or not model or model == "auto":
        return None
    return getattr(_catalog, "EXCLUDED_MODELS", {}).get(model)


def _models_cache_path() -> Path:
    home = os.getenv("ACCTOKEN_IMAGEGEN_HOME", "").strip()
    root = Path(os.path.expanduser(home)) if home else Path.home() / ".agents"
    return root / "cache" / "acctoken-imagegen" / "models.json"


def _accessible_models(*, refresh: bool = False) -> set[str] | None:
    """Model ids this API key may use, or None when that cannot be determined.

    Cached under the runtime home (never inside the skill directory). Set
    ACCTOKEN_IMAGEGEN_OFFLINE=1 to skip the probe entirely, or
    ACCTOKEN_IMAGEGEN_MODELS_TTL to change the cache lifetime.
    """
    if os.getenv("ACCTOKEN_IMAGEGEN_OFFLINE", "").strip() in ("1", "true", "yes"):
        return None
    ttl = float(os.getenv("ACCTOKEN_IMAGEGEN_MODELS_TTL", "") or DEFAULT_MODELS_TTL)
    path = _models_cache_path()
    if not refresh and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("ts", 0)) < ttl:
                return set(payload.get("models", []))
        except (OSError, ValueError, TypeError):
            pass

    key, _ = _resolve_api_key()
    if not key:
        return None
    try:
        import httpx  # pyright: ignore[reportMissingImports]

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(
                DEFAULT_BASE_URL.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", not fatal
        _warn(f"Could not list relay models ({exc}); assuming the full catalog is available.")
        return None

    ids = sorted({str(m.get("id")) for m in data.get("data", []) if m.get("id")})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "models": ids}), encoding="utf-8")
    except OSError:
        pass
    return set(ids)


def _accessible_catalog_ids(accessible: set[str] | None) -> list[str]:
    if _catalog is None:
        return []
    ids = [
        m.id
        for m in _catalog.CATALOG.values()  # type: ignore[union-attr]
        if m.endpoint in _catalog.SUPPORTED_ENDPOINTS  # type: ignore[union-attr]
        and (accessible is None or m.id in accessible)
    ]
    return sorted(ids)


def _classify_task(args: argparse.Namespace, *, has_image: bool) -> str:
    """Infer the task category when the caller did not pass --task."""
    explicit = (getattr(args, "task", None) or DEFAULT_TASK or "").strip().lower()
    if explicit and explicit != "auto":
        return explicit
    command = getattr(args, "command", "") or ""
    if command == "edit":
        return "edit"
    if has_image:
        return "edit"  # generate with an input image == reference / precise edit
    return "general"


def _default_size_for(entry: Any, task: str) -> str | None:
    if entry is None:
        return None
    if task == "4k" and getattr(entry, "size_4k", ""):
        return entry.size_4k
    return getattr(entry, "size_default", "") or None


def _resolve_request(
    args: argparse.Namespace, *, has_image: bool, input_images: int = 0
) -> dict[str, Any]:
    """Resolve --model auto to a concrete model, plus the params that follow from it."""
    pinned = (getattr(args, "model", None) or DEFAULT_MODEL) or DEFAULT_MODEL
    entry = _catalog_entry(pinned)

    excluded = _catalog_excluded_reason(pinned)
    if excluded:
        _die(
            f"{pinned} is no longer offered by this skill: {excluded}. "
            "Leave --model at auto, or pin a catalog id (`models` lists them)."
        )

    # No catalog, or an id the catalog does not know: pass it through unchanged.
    if _catalog is None or (pinned != "auto" and entry is None):
        return {
            "model": pinned,
            "entry": entry,
            "reason": "model passed through unchanged",
            "estimated_cny": None,
            "size_default": None,
            "accepts_auto_size": True,
            "n_cap": None,
            "endpoint": "images",
            "thinking": None,
            "from_catalog": False,
        }

    accessible = _accessible_models()
    if accessible is not None and pinned != "auto" and pinned not in accessible:
        _warn(
            f"{pinned} is not in this API key's model list (/v1/models); "
            "the relay will likely reject it with a permission error."
        )

    task = _classify_task(args, has_image=has_image)
    profile = (getattr(args, "profile", None) or DEFAULT_PROFILE or "cheapest").lower()
    background = _normalize_size_name(getattr(args, "background", "") or "")
    n = _coerce_int(getattr(args, "n", 1) or 1, "n")
    size = getattr(args, "size", None)
    quality = getattr(args, "quality", None)
    # Thinking tokens are billed as output. The allowance is passed through to every
    # candidate; choose_model applies it only to models that advertise `thinking`, so
    # a model without the knob is never penalised for a flag it ignores.
    thinking_requested = getattr(args, "thinking", None)
    thinking_allowance = THINKING_TOKENS.get(thinking_requested or "", 0)

    model_obj, reason = _catalog.choose_model(  # type: ignore[union-attr]
        task,
        profile=profile,
        n=n,
        transparent=(background == "transparent"),
        needs_image=has_image,
        accessible=accessible,
        max_cost_cny=getattr(args, "max_cost", None),
        size=size,
        quality=quality,
        input_images=input_images,
        thinking_tokens=thinking_allowance,
        pinned_model=pinned,
    )
    if model_obj is None:
        avail = _accessible_catalog_ids(accessible)
        hint = f" Accessible image models: {', '.join(avail)}." if avail else ""
        fallback = {
            "cutout": " For a transparent asset without image-cutout, generate on a flat "
            "chroma-key background and run scripts/run_chroma.sh.",
            "upscale": " For upscaling without image-hd, resize locally with Pillow.",
            "layers": " Layer decomposition needs seedream-5.0-pro-layers enabled on the key.",
        }.get(task, "")
        _die(
            f"Could not auto-select a model: {reason}.{hint}{fallback} "
            "Pin one with --model, relax with --profile balanced, or raise --max-cost."
        )

    per_call_n = min(n, model_obj.n_max) if model_obj.n_max else n
    _ = per_call_n  # kept for readability; the CLI fans n out itself
    default_size = _default_size_for(model_obj, task)
    est = _catalog.estimate_cost_cny(  # type: ignore[union-attr]
        model_obj,
        size=size or default_size,
        quality=quality,
        n=n,
        input_images=input_images,
        thinking_tokens=thinking_allowance if model_obj.thinking else 0,
    )
    return {
        "model": model_obj.id,
        "entry": model_obj,
        "reason": reason,
        "estimated_cny": est,
        "size_default": default_size,
        "accepts_auto_size": model_obj.accepts_auto_size,
        "n_cap": model_obj.n_max,
        "endpoint": model_obj.endpoint,
        "thinking": _thinking_for(args, model_obj),
        "from_catalog": True,
    }


def _thinking_for(args: argparse.Namespace, entry: Any) -> str | None:
    """The thinking level to send for a chosen model, or None to omit the knob.

    Only models that advertise `thinking` accept it. On gemini-nano-banana-2.1 the
    relay bills thinking tokens as output, so `minimal` (the default) is the cheap
    setting: measured ~820 extra tokens, about ¥0.13 at 1K, when set to `high`.
    """
    requested = getattr(args, "thinking", None)
    if entry is None or not getattr(entry, "thinking", False):
        if requested not in (None, DEFAULT_THINKING):
            _warn("--thinking only applies to models with a thinking knob; ignoring it.")
        return None
    return requested or DEFAULT_THINKING


def _resolve_size(args: argparse.Namespace, resolved: dict[str, Any]) -> str | None:
    """The size to put on the wire for generate: explicit, else the model default."""
    if getattr(args, "size", None):
        return args.size
    default = resolved.get("size_default")
    if default and _normalize_size_name(default) != "auto":
        return default
    if resolved.get("accepts_auto_size"):
        return "auto"
    return None


def _resolve_watermark(args: argparse.Namespace, resolved: dict[str, Any]) -> bool | None:
    """Whether to send watermark=false. Models that watermark by default are
    opted out of, because a visible 'AI generated' stamp makes an asset unusable;
    pass --watermark to keep the relay default."""
    entry = resolved.get("entry")
    if entry is None or not getattr(entry, "watermark", False):
        return None
    explicit = getattr(args, "watermark", None)
    if explicit is not None:
        return explicit
    return False


def _describe_selection(args: argparse.Namespace, resolved: dict[str, Any]) -> None:
    if not resolved.get("from_catalog"):
        return
    est = resolved.get("estimated_cny")
    cost = f", ~CNY {est:.2f}" if isinstance(est, (int, float)) else ""
    print(f"Selected {resolved['model']} ({resolved.get('reason', '')}{cost}).", file=sys.stderr)


def _resolve_api_key() -> tuple[str, str]:
    for var in API_KEY_ENV_VARS:
        value = os.getenv(var)
        if value:
            return value, f"${var}"
    for path in _key_file_candidates():
        file_key = _read_key_from(path)
        if file_key:
            return file_key, str(path)
    return "", ""


def _ensure_api_key(dry_run: bool) -> None:
    key, source = _resolve_api_key()
    if key:
        print(f"API key resolved from {source}.", file=sys.stderr)
        return
    if dry_run:
        _warn("No API key available; dry-run only.")
        return
    _die(
        "No API key available. Set ACCTOKEN_API_KEY (or IMAGE_API_KEY / OPENAI_API_KEY), "
        "or create " + str(_key_file_path()) + " (ACCTOKEN_IMAGE_ENV overrides this path)."
    )


def _read_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    if prompt:
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            _die(f"Image file not found: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Image exceeds 50MB limit: {path}")
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: str | None) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower()
    if fmt not in {"png", "jpeg", "jpg", "webp"}:
        _die("output-format must be png, jpeg, jpg, or webp.")
    return "jpeg" if fmt == "jpg" else fmt


def _normalize_size_name(size: Any) -> str:
    """Lowercase/strip a raw --size value so keyword checks such as 'auto' are forgiving."""
    return str(size).strip().lower()


def _parse_size(size: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([1-9][0-9]*)\s*[xX*]\s*([1-9][0-9]*)", size)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _validate_gpt_image_2_size(size: str, *, label: str = "") -> None:
    if size == "auto":
        return

    parsed = _parse_size(size)
    if parsed is None:
        _fail(label, "size must be auto or WIDTHxHEIGHT, for example 1024x1024.")

    width, height = parsed
    max_edge = max(width, height)
    min_edge = min(width, height)
    total_pixels = width * height

    if max_edge > GPT_IMAGE_2_MAX_EDGE:
        _fail(
            label,
            "this model size maximum edge length must be less than or equal to 3840px.",
        )
    if width % 16 != 0 or height % 16 != 0:
        _fail(label, "size width and height must be multiples of 16px.")
    if max_edge / min_edge > GPT_IMAGE_2_MAX_RATIO:
        _fail(label, "size long edge to short edge ratio must not exceed 3:1.")
    if total_pixels < GPT_IMAGE_2_MIN_PIXELS or total_pixels > GPT_IMAGE_2_MAX_PIXELS:
        _fail(
            label,
            "size total pixels must be at least 655,360 and no more than 8,294,400.",
        )


def _size_keyword(size: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[kK]", str(size).strip()))


_RATIO_SPEC = re.compile(r"^\s*([1-9][0-9]*)\s*:\s*([1-9][0-9]*)\s*$")
_TIER_RATIO_SPEC = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?[kK])\s*@\s*([1-9][0-9]*)\s*:\s*([1-9][0-9]*)\s*$"
)


def _ratio_spec(size: Any) -> str | None:
    """Normalise `16:9` / `16 : 9` to `16:9`. None when it is not an aspect ratio."""
    match = _RATIO_SPEC.match(str(size))
    if not match:
        return None
    return f"{int(match.group(1))}:{int(match.group(2))}"


def _tier_ratio_spec(size: Any) -> tuple[str, str] | None:
    """Parse the Gemini `tier@ratio` form, e.g. `2K@16:9` -> ("2K", "16:9")."""
    match = _TIER_RATIO_SPEC.match(str(size))
    if not match:
        return None
    tier = f"{match.group(1).rstrip('kK')}K"
    return tier, f"{int(match.group(2))}:{int(match.group(3))}"


def _validate_catalog_size(entry: Any, width: int, height: int, *, label: str = "") -> None:
    mid = getattr(entry, "id", "model")
    max_edge = max(width, height)
    min_edge = min(width, height)
    pixels = width * height
    mult = getattr(entry, "size_multiple_of", None)
    if mult and (width % mult or height % mult):
        _fail(label, f"{mid} size width and height must be multiples of {mult}px.")
    if getattr(entry, "size_min_edge", None) and min_edge < entry.size_min_edge:
        _fail(label, f"{mid} size edges must be at least {entry.size_min_edge}px.")
    if getattr(entry, "size_max_edge", None) and max_edge > entry.size_max_edge:
        _fail(label, f"{mid} size maximum edge must be <= {entry.size_max_edge}px.")
    if getattr(entry, "size_min_pixels", None) and pixels < entry.size_min_pixels:
        _fail(
            label,
            f"{mid} size needs at least {entry.size_min_pixels:,} total pixels "
            f"(for example {entry.size_default or '1280x720'}).",
        )
    if getattr(entry, "size_max_pixels", None) and pixels > entry.size_max_pixels:
        _fail(label, f"{mid} size total pixels must be <= {entry.size_max_pixels:,}.")
    ratio = getattr(entry, "size_max_ratio", None)
    if ratio and max_edge / min_edge > ratio:
        _fail(label, f"{mid} size long edge to short edge ratio must not exceed {ratio:g}:1.")


def _validate_size(size: str, model: str, *, label: str = "") -> None:
    name = _normalize_size_name(size)
    entry = _catalog_entry(model)

    if name == "auto":
        if entry is not None and not getattr(entry, "accepts_auto_size", False):
            default = getattr(entry, "size_default", "") or "1024x1024"
            _fail(
                label,
                f"size 'auto' is not accepted by {model} (the relay only allows it for "
                f"layer_decomposition). Pass an explicit size such as --size {default}.",
            )
        if entry is None and not _is_gpt_image_2_family(model):
            _warn(
                "size 'auto' is only accepted by some models; pass an explicit size to be safe."
            )
        return

    if _size_keyword(name):
        allowed = tuple(getattr(entry, "size_keywords", ()) or ()) if entry else ()
        if entry is not None:
            if name.upper() not in {k.upper() for k in allowed}:
                if allowed:
                    opts = ", ".join(sorted(allowed))
                    _fail(
                        label,
                        f"size keyword {size!r} is not supported by {model}; "
                        f"use one of {opts}, or WIDTHxHEIGHT.",
                    )
                _fail(
                    label,
                    f"{model} does not accept size keywords such as {size!r}; use WIDTHxHEIGHT.",
                )
        return

    ratio = _ratio_spec(name)
    if ratio is not None:
        allowed = tuple(getattr(entry, "aspect_ratios", ()) or ()) if entry else ()
        if allowed:
            if ratio not in allowed:
                _fail(
                    label,
                    f"{model} accepts the aspect ratios {', '.join(allowed)}; "
                    f"{size!r} is not one of them.",
                )
            return
        _fail(
            label,
            f"size {size!r} is an aspect ratio, which only the Gemini endpoint accepts; "
            "use WIDTHxHEIGHT at a 1:1 aspect for this model.",
        )

    compound = _tier_ratio_spec(name)
    if compound is not None:
        tiers = tuple(getattr(entry, "image_tiers", ()) or ()) if entry else ()
        ratios = tuple(getattr(entry, "aspect_ratios", ()) or ()) if entry else ()
        if not ratios:
            _fail(
                label,
                f"size {size!r} uses the tier@ratio form, which only the Gemini endpoint "
                "accepts; use WIDTHxHEIGHT for this model.",
            )
        tier, want_ratio = compound
        if tier.upper() not in {t.upper() for t in tiers}:
            _fail(label, f"{model} accepts the tiers {', '.join(tiers)}; {tier!r} is not one of them.")
        if want_ratio not in ratios:
            _fail(
                label,
                f"{model} accepts the aspect ratios {', '.join(ratios)}; "
                f"{want_ratio!r} is not one of them.",
            )
        return

    parsed = _parse_size(size)
    if parsed is None:
        _fail(
            label,
            "size must be WIDTHxHEIGHT (WIDTH*HEIGHT accepted), for example 1024x1024.",
        )
    width, height = parsed

    if entry is not None:
        _validate_catalog_size(entry, width, height, label=label)
        return

    # ---- legacy fallbacks for model ids that are not in the catalog ----
    if _is_gpt_image_2_family(model):
        _validate_gpt_image_2_size(size, label=label)
        return

    if model.startswith(GPT_IMAGE_MODEL_PREFIX):
        if name not in ALLOWED_LEGACY_SIZES:
            _fail(
                label,
                "size must be one of 1024x1024, 1536x1024, 1024x1536, or auto for this GPT Image model.",
            )
        return

    # Open/permissive models (for example seedream-5.0-flash when the catalog is
    # unavailable): accept auto or any WIDTHxHEIGHT (also WIDTH*HEIGHT) within loose bounds.
    if width < OPEN_MODEL_MIN_EDGE or height < OPEN_MODEL_MIN_EDGE:
        _fail(label, f"size edges must be at least {OPEN_MODEL_MIN_EDGE}px.")
    if width > OPEN_MODEL_MAX_EDGE or height > OPEN_MODEL_MAX_EDGE:
        _fail(label, f"size edges must be at most {OPEN_MODEL_MAX_EDGE}px.")
    if max(width, height) / min(width, height) > OPEN_MODEL_MAX_RATIO:
        _fail(
            label,
            f"size long edge to short edge ratio must not exceed {OPEN_MODEL_MAX_RATIO:g}:1.",
        )


def _validate_quality(quality: str, *, label: str = "") -> None:
    if quality not in ALLOWED_QUALITIES:
        _fail(label, "quality must be one of low, medium, high, or auto.")


def _validate_background(background: str | None, *, label: str = "") -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _fail(label, "background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: str | None, *, label: str = "") -> None:
    if input_fidelity not in ALLOWED_INPUT_FIDELITIES:
        _fail(label, "input-fidelity must be one of low or high.")


def _validate_model(model: str, *, label: str = "") -> None:
    if model == "auto":
        return
    if not model or not model.strip():
        _fail(label, "model must be a non-empty model id, for example seedream-5.0-flash.")
    if not any(model.startswith(prefix) for prefix in KNOWN_MODEL_PREFIXES):
        _warn(
            f"Unrecognized model id {model!r}; passing it through to the relay unchanged."
        )


def _validate_transparency(
    background: str | None, output_format: str, *, label: str = ""
) -> None:
    if background == "transparent" and output_format not in {"png", "webp"}:
        _fail(label, "transparent background requires output-format png or webp.")


def _validate_model_specific_options(
    *,
    model: str,
    input_fidelity: str | None = None,
    label: str = "",
) -> None:
    if _is_gpt_image_2_family(model):
        if input_fidelity is not None:
            _fail(
                label,
                f"input_fidelity is not supported in {model} because image inputs always use high fidelity for this model.",
            )
        return

    # Documented as unsupported for the qwen-image family. Reject locally instead of
    # letting the relay return an error or silently ignore the option.
    if model.startswith("qwen-image-") and input_fidelity is not None:
        _fail(label, f"input_fidelity is not supported in {model}. Remove --input-fidelity.")


def _validate_generate_payload(payload: dict[str, Any], *, label: str = "") -> None:
    model = str(payload.get("model", DEFAULT_MODEL))
    _validate_model(model, label=label)
    n = _coerce_int(payload.get("n", 1), "n", label=label)
    if n < 1 or n > 10:
        _fail(label, "n must be between 1 and 10")
    size = payload.get("size")
    quality = str(payload.get("quality", DEFAULT_QUALITY))
    background = payload.get("background")
    if size is not None:
        _validate_size(str(size), model, label=label)
    _validate_quality(quality, label=label)
    _validate_background(background, label=label)
    _validate_model_specific_options(model=model, label=label)
    oc = payload.get("output_compression")
    if oc is not None and not (0 <= _coerce_int(oc, "output_compression", label=label) <= 100):
        _fail(label, "output_compression must be between 0 and 100")


def _build_output_paths(
    out: str,
    output_format: str,
    count: int,
    out_dir: str | None,
) -> list[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir)
        out_base.mkdir(parents=True, exist_ok=True)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.exists() and out_path.is_dir():
        out_path.mkdir(parents=True, exist_ok=True)
        return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    elif output_format and out_path.suffix.lstrip(".").lower() != output_format:
        _warn(
            f"Output extension {out_path.suffix} does not match output-format {output_format}."
        )

    if count == 1:
        return [out_path]

    return [
        out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
        for i in range(1, count + 1)
    ]


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    fields = _fields_from_args(args)
    return _augment_prompt_fields(args.augment, prompt, fields)


def _augment_prompt_fields(
    augment: bool, prompt: str, fields: dict[str, str | None]
) -> str:
    if not augment:
        return prompt

    sections: list[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("scene"):
        sections.append(f"Scene/background: {fields['scene']}")
    if fields.get("subject"):
        sections.append(f"Subject: {fields['subject']}")
    if fields.get("style"):
        sections.append(f"Style/medium: {fields['style']}")
    if fields.get("composition"):
        sections.append(f"Composition/framing: {fields['composition']}")
    if fields.get("lighting"):
        sections.append(f"Lighting/mood: {fields['lighting']}")
    if fields.get("palette"):
        sections.append(f"Color palette: {fields['palette']}")
    if fields.get("materials"):
        sections.append(f"Materials/textures: {fields['materials']}")
    if fields.get("text"):
        sections.append(f'Text (verbatim): "{fields["text"]}"')
    if fields.get("constraints"):
        sections.append(f"Constraints: {fields['constraints']}")
    if fields.get("negative"):
        sections.append(f"Avoid: {fields['negative']}")

    return "\n".join(sections)


def _fields_from_args(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "use_case": getattr(args, "use_case", None),
        "scene": getattr(args, "scene", None),
        "subject": getattr(args, "subject", None),
        "style": getattr(args, "style", None),
        "composition": getattr(args, "composition", None),
        "lighting": getattr(args, "lighting", None),
        "palette": getattr(args, "palette", None),
        "materials": getattr(args, "materials", None),
        "text": getattr(args, "text", None),
        "constraints": getattr(args, "constraints", None),
        "negative": getattr(args, "negative", None),
    }


def _download_as_base64(url: str) -> str:
    # The URL comes from the API response, not from the user, so pin the scheme and
    # use the SDK's own HTTP client (httpx) rather than urllib: it enforces timeouts,
    # validates TLS, and cannot be steered into file:// or other local handlers.
    if not url.lower().startswith(("http://", "https://")):
        _die(f"Refusing to download image from a non-HTTP URL: {url[:120]}")
    try:
        import httpx  # pyright: ignore[reportMissingImports]
    except ImportError:
        _die(f"Downloading image URLs requires httpx. {_dependency_hint('httpx')}")
    try:
        with httpx.stream(
            "GET", url, timeout=120.0, follow_redirects=True
        ) as resp:  # trust_env stays on: match the SDK's proxy behaviour
            resp.raise_for_status()
            data = resp.read()
    except httpx.HTTPError as exc:
        _die(f"Image download failed for {url[:120]}: {exc}")
    return base64.b64encode(data).decode("ascii")


def _extract_images(result: Any) -> list[str]:
    images: list[str] = []
    for item in result.data:
        b64 = getattr(item, "b64_json", None)
        if b64:
            images.append(b64)
            continue
        url = getattr(item, "url", None)
        if url:
            images.append(_download_as_base64(url))
            continue
        _die("Relay returned neither b64_json nor url for an image result.")
    return images


# --------------------------------------------------------------------------- #
# Gemini native transport
#
# gemini-nano-banana-2.1 is not served over /v1/images/generations. The relay
# exposes it at POST /v1beta/models/<id>:generateContent, and that is the only
# path that actually honours the request: the OpenAI-compatible chat surface
# silently ignores image_config and returns 1376x768 for every aspect ratio.
# Measured on 2026-10-17 -- see references/image-api.md.
# --------------------------------------------------------------------------- #
def _api_root() -> str:
    """The relay origin, with the /v1 or /v1beta suffix stripped."""
    base = DEFAULT_BASE_URL.rstrip("/")
    for suffix in ("/v1beta", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def gemini_endpoint(model: str) -> str:
    return f"{_api_root()}/v1beta/models/{model}:generateContent"


def _gemini_size(size: str | None, entry: Any) -> tuple[str, str]:
    """Map a --size value onto (aspect_ratio, image_size) for the Gemini endpoint.

    Gemini takes a ratio + a tier instead of pixel dimensions, so `--size 2048x1152`
    becomes ("16:9", "2K") and `--size 2K` becomes ("1:1", "2K"). The tier thresholds
    match how the vendor bills: 1K=1120, 2K=1680, 4K=3780 image tokens.
    """
    ratios = tuple(getattr(entry, "aspect_ratios", ()) or ()) or ("1:1",)
    tiers = tuple(getattr(entry, "image_tiers", ()) or ()) or ("1K",)
    default_tier = str(getattr(entry, "size_default", "") or tiers[0])

    def _tier(name: str) -> str:
        for t in tiers:
            if t.upper() == str(name).upper():
                return t
        return tiers[0]

    if size is None:
        return ratios[0], _tier(default_tier)

    raw = str(size).strip()
    if _normalize_size_name(raw) == "auto":
        _die(
            f"--size auto is not accepted by this model. Use one of {'/'.join(tiers)}, "
            "one of " + "/".join(ratios) + " for the aspect ratio, WIDTHxHEIGHT, or omit --size."
        )
    for t in tiers:  # a bare tier keyword such as 2K
        if raw.upper() == t.upper():
            return ratios[0], t

    compound = _tier_ratio_spec(raw)
    if compound is not None:
        tier, want_ratio = compound
        tier = _tier(tier)
        for allowed in ratios:
            if allowed == want_ratio:
                return allowed, tier
        _die(
            f"aspect ratio {want_ratio} is not supported by this model; "
            f"use one of {', '.join(ratios)}."
        )

    ratio = _ratio_spec(raw)
    if ratio is not None:
        for allowed in ratios:
            if allowed == ratio:
                return ratio, _tier(default_tier)
        _die(
            f"aspect ratio {ratio} is not supported by this model; "
            f"use one of {', '.join(ratios)}."
        )

    parsed = _parse_size(raw)
    if parsed is None:
        _die(
            f"size must be one of {'/'.join(tiers)}, one of {', '.join(ratios)} for the "
            "aspect ratio, WIDTHxHEIGHT, or omitted."
        )
    width, height = parsed
    if _catalog is not None:
        ratio = _catalog.aspect_ratio_for(width, height, ratios)  # type: ignore[union-attr]
        tier = _catalog.image_tier_for(width, height, tiers)  # type: ignore[union-attr]
    else:  # pragma: no cover - the catalog ships with the skill
        ratio, tier = ratios[0], _tier(default_tier)
    return ratio, tier


def _gemini_parts(prompt: str, image_paths: list[Path]) -> list[dict[str, Any]]:
    """Build `parts`: the prompt, then one inlineData part per reference image."""
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for path in image_paths:
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


def _gemini_body(
    prompt: str,
    image_paths: list[Path],
    *,
    aspect_ratio: str,
    image_size: str,
    thinking_level: str | None,
) -> dict[str, Any]:
    generation_config: dict[str, Any] = {
        "imageConfig": {"aspectRatio": aspect_ratio, "imageSize": image_size}
    }
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    return {
        "contents": [{"parts": _gemini_parts(prompt, image_paths)}],
        "generationConfig": generation_config,
    }


def _extract_gemini_images(payload: dict[str, Any]) -> list[str]:
    """Base64 images from one generateContent response, final image only.

    The model emits a low-resolution draft and then the real image as successive
    `parts` of the same candidate; only the last one is the answer.
    """
    images: list[str] = []
    for candidate in payload.get("candidates") or []:
        parts = ((candidate.get("content") or {}).get("parts")) or []
        found: list[str] = []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            if data:
                found.append(str(data))
        if found:
            images.append(found[-1])
    return images


def _gemini_request_error(model: str, resp: Any) -> str:
    detail = ""
    try:
        detail = resp.text[:400]
    except Exception:  # noqa: BLE001 - a body we cannot read must not mask the status
        detail = "<unreadable response body>"
    return f"{model} generateContent returned HTTP {resp.status_code}: {detail}"


def _call_gemini(model: str, body: dict[str, Any], *, timeout: float = 300.0) -> dict[str, Any]:
    """POST one generateContent request and return the parsed JSON."""
    try:
        import httpx  # pyright: ignore[reportMissingImports]
    except ImportError:
        _die(f"The Gemini endpoint requires httpx. {_dependency_hint('httpx')}")
    url = gemini_endpoint(model)
    key, _ = _resolve_api_key()
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{model} request failed: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(_gemini_request_error(model, resp))
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{model} returned a non-JSON response: {exc}") from exc


async def _call_gemini_async(
    model: str, body: dict[str, Any], *, timeout: float = 300.0
) -> dict[str, Any]:
    try:
        import httpx  # pyright: ignore[reportMissingImports]
    except ImportError:
        _die(f"The Gemini endpoint requires httpx. {_dependency_hint('httpx')}")
    url = gemini_endpoint(model)
    key, _ = _resolve_api_key()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {key}"}
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{model} request failed: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(_gemini_request_error(model, resp))
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{model} returned a non-JSON response: {exc}") from exc


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _decode_and_write(images: list[str], outputs: list[Path], force: bool) -> None:
    for idx, image_b64 in enumerate(images):
        if idx >= len(outputs):
            break
        out_path = outputs[idx]
        if out_path.exists() and not force:
            _die(f"Output already exists: {out_path} (use --force to overwrite)")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(image_b64))
        print(f"Wrote {out_path}")


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _downscale_image_bytes(
    image_bytes: bytes, *, max_dim: int, output_format: str
) -> bytes:
    try:
        # Third-party deps are installed in the skill's own environment, resolved at
        # runtime by scripts/_resolve_python.sh. An editor or checker rooted elsewhere
        # cannot see it, so these imports are expected to look unresolved statically;
        # each site still fails closed with an actionable message.
        from PIL import Image  # pyright: ignore[reportMissingImports]
    except ImportError:
        _die(f"Downscaling requires Pillow. {_dependency_hint('pillow')}")

    if max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, float(max_dim) / float(max(w, h)))
        target = (max(1, round(w * scale)), max(1, round(h * scale)))

        resized = (
            img if target == (w, h) else img.resize(target, Image.Resampling.LANCZOS)
        )

        fmt = output_format.lower()
        if fmt == "jpg":
            fmt = "jpeg"

        if fmt == "jpeg":
            if resized.mode in ("RGBA", "LA") or (
                "transparency" in getattr(resized, "info", {})
            ):
                bg = Image.new("RGB", resized.size, (255, 255, 255))
                bg.paste(
                    resized.convert("RGBA"), mask=resized.convert("RGBA").split()[-1]
                )
                resized = bg
            else:
                resized = resized.convert("RGB")

        out = BytesIO()
        resized.save(out, format=fmt.upper())
        return out.getvalue()


def _decode_write_and_downscale(
    images: list[str],
    outputs: list[Path],
    *,
    force: bool,
    downscale_max_dim: int | None,
    downscale_suffix: str,
    output_format: str,
) -> None:
    for idx, image_b64 in enumerate(images):
        if idx >= len(outputs):
            break
        out_path = outputs[idx]
        if out_path.exists() and not force:
            _die(f"Output already exists: {out_path} (use --force to overwrite)")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        raw = base64.b64decode(image_b64)
        out_path.write_bytes(raw)
        print(f"Wrote {out_path}")

        if downscale_max_dim is None:
            continue

        derived = _derive_downscale_path(out_path, downscale_suffix)
        if derived.exists() and not force:
            _die(f"Output already exists: {derived} (use --force to overwrite)")
        derived.parent.mkdir(parents=True, exist_ok=True)
        resized = _downscale_image_bytes(
            raw, max_dim=downscale_max_dim, output_format=output_format
        )
        derived.write_bytes(resized)
        print(f"Wrote {derived}")


def _create_client():
    try:
        from openai import OpenAI  # pyright: ignore[reportMissingImports]
    except ImportError:
        _die(
            f"openai SDK not installed in the active environment. {_dependency_hint('openai')}"
        )
    return OpenAI(base_url=DEFAULT_BASE_URL, api_key=_resolve_api_key()[0])


def _create_async_client():
    try:
        from openai import AsyncOpenAI  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            import openai as _openai  # pyright: ignore[reportMissingImports]  # noqa: F401
        except ImportError:
            _die(
                f"openai SDK not installed in the active environment. {_dependency_hint('openai')}"
            )
        _die(
            "AsyncOpenAI not available in this openai SDK version. "
            f"{_dependency_hint('openai', upgrade=True)}"
        )
    return AsyncOpenAI(base_url=DEFAULT_BASE_URL, api_key=_resolve_api_key()[0])


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] if value else "job"


def _normalize_job(job: Any, idx: int) -> dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        if "prompt" not in job or not str(job["prompt"]).strip():
            _die(f"Missing prompt for job {idx}")
        return job
    _die(f"Invalid job at index {idx}: expected string or object.")
    return {}  # unreachable


def _read_jobs_jsonl(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        _die(f"Input file not found: {p}")
    jobs: list[dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any
            if line.startswith("{"):
                item = json.loads(line)
            else:
                item = line
            jobs.append(_normalize_job(item, idx=line_no))
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSON on line {line_no}: {exc}")
    if not jobs:
        _die("No jobs found in input file.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _merge_non_null(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    merged = dict(dst)
    for k, v in src.items():
        if v is not None:
            merged[k] = v
    return merged


def _job_output_paths(
    *,
    out_dir: Path,
    output_format: str,
    idx: int,
    prompt: str,
    n: int,
    explicit_out: str | None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "." + output_format

    if explicit_out:
        base = Path(explicit_out)
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif base.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        slug = _slugify(prompt[:80])
        base = out_dir / f"{idx:03d}-{slug}{ext}"

    if n == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, n + 1)]


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    # Best-effort: openai SDK errors vary by version. Prefer a conservative fallback.
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    msg = str(exc)
    m = re.search(r"retry[- ]after[:= ]+([0-9]+(?:\.[0-9]+)?)", msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


def _is_transient_error(exc: Exception) -> bool:
    if _is_rate_limit_error(exc):
        return True
    name = exc.__class__.__name__.lower()
    if "timeout" in name or "timedout" in name or "tempor" in name:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg or "connection reset" in msg


async def _generate_one_with_retries(
    client: Any,
    payload: dict[str, Any],
    *,
    attempts: int,
    job_label: str,
) -> Any:
    return await _retry_async(
        lambda: client.images.generate(**payload), attempts=attempts, job_label=job_label
    )


async def _retry_async(call: Any, *, attempts: int, job_label: str) -> Any:
    """Run an awaitable factory, retrying transient failures with backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_error(exc):
                raise
            if attempt == attempts:
                raise
            sleep_s = _extract_retry_after_seconds(exc)
            if sleep_s is None:
                sleep_s = min(60.0, 2.0**attempt)
            print(
                f"{job_label} attempt {attempt}/{attempts} failed ({exc.__class__.__name__}); retrying in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(sleep_s)
    raise last_exc or RuntimeError("unknown error")


async def _run_generate_batch(args: argparse.Namespace) -> int:
    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir)

    base_fields = _fields_from_args(args)
    base_resolved = _resolve_request(args, has_image=False)
    base_model = base_resolved["model"]
    if not args.dry_run:
        _describe_selection(args, base_resolved)

    def plan_job(job: dict[str, Any]) -> dict[str, Any]:
        """Merge CLI defaults with per-job overrides into one request plan."""
        model_id = str(job.get("model") or base_model)
        _validate_model(model_id)
        entry = _catalog_entry(model_id)

        n = _coerce_int(job.get("n", args.n), "n")
        if n < 1 or n > 10:
            _die("n must be between 1 and 10")

        size = job.get("size", args.size)
        if size is None:
            # The Gemini endpoint's natural vocabulary is a tier plus a ratio; accept
            # those as first-class per-job keys and fold them into the tier@ratio form.
            job_ratio = job.get("aspect_ratio")
            job_tier = job.get("image_size")
            if job_ratio or job_tier:
                tiers = tuple(getattr(entry, "image_tiers", ()) or ())
                ratios = tuple(getattr(entry, "aspect_ratios", ()) or ())
                if not ratios:
                    _die(
                        f"job {job.get('__idx', '')}: aspect_ratio/image_size only apply to "
                        "models served over the Gemini endpoint."
                    )
                tier = str(job_tier or (getattr(entry, "size_default", "") or tiers[0]))
                size = f"{tier}@{job_ratio or ratios[0]}"
        if size is None and entry is not None:
            default = getattr(entry, "size_default", "") or None
            if default and _normalize_size_name(default) != "auto":
                size = default
        if size is not None:
            _validate_size(str(size), model_id)

        quality = str(job.get("quality", args.quality))
        _validate_quality(quality)
        background = job.get("background", args.background)
        output_format = _normalize_output_format(job.get("output_format", args.output_format))
        _validate_transparency(background, output_format)

        watermark = None
        if entry is not None and getattr(entry, "watermark", False):
            watermark = args.watermark if args.watermark is not None else False

        return {
            "model": model_id,
            "entry": entry,
            "n": n,
            "size": size,
            "quality": quality,
            "background": background,
            "output_format": output_format,
            "watermark": watermark,
        }

    def build_payload(job_plan: dict[str, Any], prompt: str, chunk: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": job_plan["model"],
            "prompt": prompt,
            "n": chunk,
            "size": job_plan["size"],
            "quality": job_plan["quality"],
            "background": job_plan["background"],
            "output_format": job_plan["output_format"],
            "output_compression": args.output_compression,
            "moderation": args.moderation,
        }
        if job_plan["watermark"] is not None:
            payload["watermark"] = job_plan["watermark"]
        return {k: v for k, v in payload.items() if v is not None}

    def build_gemini_body(job_plan: dict[str, Any], prompt: str) -> dict[str, Any]:
        entry = job_plan["entry"]
        aspect_ratio, image_size = _gemini_size(job_plan["size"], entry)
        return _gemini_body(
            prompt,
            [],
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            thinking_level=_thinking_for(args, entry),
        )

    def augment(job: dict[str, Any], prompt: str) -> str:
        fields = _merge_non_null(base_fields, job.get("fields", {}))
        fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields})
        return _augment_prompt_fields(args.augment, prompt, fields)

    if args.dry_run:
        for i, job in enumerate(jobs, start=1):
            prompt = str(job["prompt"]).strip()
            jp = plan_job(job)
            augmented = augment(job, prompt)
            chunks = _split_n(jp["n"], getattr(jp["entry"], "n_max", None))
            outputs = _job_output_paths(
                out_dir=out_dir,
                output_format=jp["output_format"],
                idx=i,
                prompt=prompt,
                n=jp["n"],
                explicit_out=job.get("out"),
            )
            downscaled = None
            if args.downscale_max_dim is not None:
                downscaled = [
                    str(_derive_downscale_path(p, args.downscale_suffix)) for p in outputs
                ]
            payload = build_payload(jp, augmented, chunks[0])
            _validate_generate_payload(payload, label=f"job {i}")
            if getattr(jp["entry"], "endpoint", "images") == "gemini":
                body = build_gemini_body(jp, augmented)
                _print_request(
                    {
                        "endpoint": gemini_endpoint(jp["model"]),
                        "job": i,
                        "mode": "generate",
                        "model": jp["model"],
                        "aspect_ratio": body["generationConfig"]["imageConfig"]["aspectRatio"],
                        "image_size": body["generationConfig"]["imageConfig"]["imageSize"],
                        "requested_n": jp["n"],
                        "calls": len(chunks),
                        "n_per_call": chunks,
                        "outputs": [str(p) for p in outputs],
                        "outputs_downscaled": downscaled,
                        "body": body,
                    }
                )
                continue
            sdk_payload, extra_body = _split_extra_body(payload)
            _print_request(
                {
                    "endpoint": "/v1/images/generations",
                    "job": i,
                    "requested_n": jp["n"],
                    "calls": len(chunks),
                    "n_per_call": chunks,
                    "outputs": [str(p) for p in outputs],
                    "outputs_downscaled": downscaled,
                    **sdk_payload,
                    **({"extra_body": extra_body} if extra_body else {}),
                }
            )
        return 0

    client = _create_async_client()
    sem = asyncio.Semaphore(args.concurrency)

    any_failed = False

    async def run_job(i: int, job: dict[str, Any]) -> tuple[int, str | None]:
        nonlocal any_failed
        prompt = str(job["prompt"]).strip()
        job_label = f"[job {i}/{len(jobs)}]"

        try:
            jp = plan_job(job)
            augmented = augment(job, prompt)
            chunks = _split_n(jp["n"], getattr(jp["entry"], "n_max", None))
            outputs = _job_output_paths(
                out_dir=out_dir,
                output_format=jp["output_format"],
                idx=i,
                prompt=prompt,
                n=jp["n"],
                explicit_out=job.get("out"),
            )
            written = 0
            for chunk in chunks:
                if getattr(jp["entry"], "endpoint", "images") == "gemini":
                    body = build_gemini_body(jp, augmented)
                    async with sem:
                        print(f"{job_label} starting (gemini)", file=sys.stderr)
                        started = time.time()
                        payload = await _retry_async(
                            lambda: _call_gemini_async(jp["model"], body),
                            attempts=args.max_attempts,
                            job_label=job_label,
                        )
                        elapsed = time.time() - started
                        print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
                    images = _extract_gemini_images(payload)
                    if not images:
                        raise RuntimeError(
                            f"{jp['model']} returned no image for {job_label}"
                        )
                    outs = outputs[written : written + len(images)]
                    _decode_write_and_downscale(
                        images,
                        outs,
                        force=args.force,
                        downscale_max_dim=args.downscale_max_dim,
                        downscale_suffix=args.downscale_suffix,
                        output_format=DEFAULT_OUTPUT_FORMAT,
                    )
                    written += len(images)
                    continue
                payload = build_payload(jp, augmented, chunk)
                _validate_generate_payload(payload, label=job_label)
                sdk_payload, extra_body = _split_extra_body(payload)
                request = dict(sdk_payload)
                request["n"] = chunk
                if extra_body:
                    request["extra_body"] = extra_body
                async with sem:
                    print(f"{job_label} starting", file=sys.stderr)
                    started = time.time()
                    result = await _generate_one_with_retries(
                        client,
                        request,
                        attempts=args.max_attempts,
                        job_label=job_label,
                    )
                    elapsed = time.time() - started
                    print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
                images = _extract_images(result)
                outs = outputs[written : written + len(images)]
                _decode_write_and_downscale(
                    images,
                    outs,
                    force=args.force,
                    downscale_max_dim=args.downscale_max_dim,
                    downscale_suffix=args.downscale_suffix,
                    output_format=jp["output_format"],
                )
                written += len(images)
            return i, None
        except Exception as exc:
            any_failed = True
            print(f"{job_label} failed: {exc}", file=sys.stderr)
            if args.fail_fast:
                raise
            return i, str(exc)

    tasks = [
        asyncio.create_task(run_job(i, job)) for i, job in enumerate(jobs, start=1)
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    return 1 if any_failed else 0


def _generate_batch(args: argparse.Namespace) -> None:
    exit_code = asyncio.run(_run_generate_batch(args))
    if exit_code:
        raise SystemExit(exit_code)


def _split_n(n: int, cap: int | None) -> list[int]:
    """Chunk a requested --n into per-call counts the model actually accepts.

    seedream and gpt-image-2.5 return HTTP 400 for n>1, so the CLI fans a request
    for several images out into several single-image calls rather than burning a
    request on a guaranteed error.
    """
    if cap is None or cap >= n:
        return [n]
    chunks: list[int] = []
    left = n
    while left > 0:
        chunk = min(cap, left)
        chunks.append(chunk)
        left -= chunk
    return chunks


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _file_to_data_url(path: Path) -> str:
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _image_field(paths: list[Path]) -> Any:
    """The `image` value for /v1/images/generations: one data URL, or a list."""
    if not paths:
        return None
    urls = [_file_to_data_url(p) for p in paths]
    return urls[0] if len(urls) == 1 else urls


def _extra_params(args: argparse.Namespace, entry: Any) -> dict[str, Any]:
    """Model-specific extras: image-cutout `scene`, image-hd `multiple`.

    The CLI flags are named --cutout-scene / --upscale-multiple so they do not
    collide with --scene, which is a prompt-augmentation hint.
    """
    mapping = {"scene": "cutout_scene", "multiple": "upscale_multiple"}
    extras: dict[str, Any] = {}
    for param, attr in mapping.items():
        value = getattr(args, attr, None)
        if value is None:
            continue
        if entry is not None and param not in tuple(getattr(entry, "extra_params", ()) or ()):
            _warn(f"--{attr.replace('_', '-')} is not a parameter of {entry.id}; ignoring it.")
            continue
        extras[param] = value
    return extras


# Parameters the relay understands but the OpenAI SDK's images.generate() signature
# does not, so they have to travel in `extra_body` instead of as keyword arguments.
EXTRA_BODY_KEYS = ("watermark", "image", "scene", "multiple")


def _split_extra_body(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    sdk = {k: v for k, v in payload.items() if k not in EXTRA_BODY_KEYS}
    extra = {k: v for k, v in payload.items() if k in EXTRA_BODY_KEYS}
    return sdk, extra


# Flags the Gemini endpoint has no equivalent for. `quality` is compared against its
# own default so that the CLI's default value never triggers a warning.
GEMINI_IGNORED_FLAGS = {
    "quality": DEFAULT_QUALITY,
    "background": None,
    "output_format": None,
    "output_compression": None,
    "moderation": None,
    "watermark": None,
    "input_fidelity": None,
}


def _warn_gemini_ignored(args: argparse.Namespace, entry: Any) -> None:
    """Name the flags the caller set that this endpoint cannot honour."""
    ignored = []
    for opt, neutral in GEMINI_IGNORED_FLAGS.items():
        if not hasattr(args, opt):
            continue
        value = getattr(args, opt)
        if value is None or value == neutral:
            continue
        ignored.append(f"--{opt.replace('_', '-')}")
    if ignored:
        _warn(
            f"{entry.id} does not take {', '.join(ignored)}; the Gemini endpoint accepts "
            "only aspect_ratio, image_size and thinking_level. The value was ignored."
        )
    if getattr(args, "watermark", None) is False:
        _warn(
            f"{entry.id} always attaches a SynthID watermark and a C2PA credential; "
            "they cannot be switched off. (They are not a visible stamp.)"
        )


def _run_gemini(
    args: argparse.Namespace,
    resolved: dict[str, Any],
    prompt: str,
    image_paths: list[Path],
    *,
    mode: str,
) -> None:
    """generate / edit against the Gemini native generateContent endpoint.

    gemini-nano-banana-2.1 does not exist on /v1/images/generations, and its request
    shape is different enough (ratio + tier + thinking, reference images as message
    parts) that it gets its own runner instead of being squeezed into the images payload.
    """
    entry = resolved.get("entry")
    model = resolved["model"]
    task = _classify_task(args, has_image=bool(image_paths))

    size = getattr(args, "size", None)
    if size is None:
        size = _default_size_for(entry, task)
    aspect_ratio, image_size = _gemini_size(size, entry)
    thinking = _thinking_for(args, entry)
    _warn_gemini_ignored(args, entry)

    body = _gemini_body(
        prompt,
        image_paths,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        thinking_level=thinking,
    )

    output_paths = _build_output_paths(
        args.out, DEFAULT_OUTPUT_FORMAT, args.n, args.out_dir
    )
    chunks = _split_n(args.n, resolved.get("n_cap"))
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [
            str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths
        ]

    if args.dry_run:
        preview = json.loads(json.dumps(body))
        ref_idx = 0
        for part in preview["contents"][0]["parts"]:
            if "inlineData" in part and ref_idx < len(image_paths):
                part["inlineData"]["data"] = f"<data URL of {image_paths[ref_idx]}>"
                ref_idx += 1
        _print_request(
            {
                "endpoint": gemini_endpoint(model),
                "mode": mode,
                "model": model,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "thinking_level": thinking,
                "requested_n": args.n,
                "calls": len(chunks),
                "n_per_call": chunks,
                "ref_images": [str(p) for p in image_paths],
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                "body": preview,
            }
        )
        return

    verb = "edit" if mode == "edit" else "generation"
    refs = f" with {len(image_paths)} reference image(s)" if image_paths else ""
    print(
        f"Calling Gemini generateContent ({verb}){refs}. "
        "This can take up to a couple of minutes.",
        file=sys.stderr,
    )
    started = time.time()
    written = 0
    for _chunk in chunks:
        payload = _call_gemini(model, body)
        images = _extract_gemini_images(payload)
        if not images:
            raise RuntimeError(
                f"{model} returned no image. Response: {json.dumps(payload)[:400]}"
            )
        outs = output_paths[written : written + len(images)]
        _decode_write_and_downscale(
            images,
            outs,
            force=args.force,
            downscale_max_dim=args.downscale_max_dim,
            downscale_suffix=args.downscale_suffix,
            output_format=DEFAULT_OUTPUT_FORMAT,
        )
        written += len(images)
    print(f"Completed in {time.time() - started:.1f}s.", file=sys.stderr)


def _generate(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    image_args = list(getattr(args, "image", None) or [])
    image_paths = _check_image_paths(image_args) if image_args else []

    resolved = _resolve_request(
        args, has_image=bool(image_paths), input_images=len(image_paths)
    )
    _describe_selection(args, resolved)
    model = resolved["model"]
    entry = resolved.get("entry")

    if resolved.get("endpoint") == "gemini":
        _run_gemini(args, resolved, prompt, image_paths, mode="generate")
        return

    size = _resolve_size(args, resolved)
    if size is not None:
        _validate_size(size, model)

    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": args.quality,
        "background": args.background,
        "output_format": output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
    }
    watermark = _resolve_watermark(args, resolved)
    if watermark is not None:
        payload["watermark"] = watermark
    payload.update(_extra_params(args, entry))
    image_field = _image_field(image_paths)
    if image_field is not None:
        payload["image"] = image_field
    payload = {k: v for k, v in payload.items() if v is not None}

    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    chunks = _split_n(args.n, resolved.get("n_cap"))
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [
            str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths
        ]

    if args.dry_run:
        sdk_payload, extra_body = _split_extra_body(payload)
        sdk_payload["n"] = chunks[0]
        if image_paths:
            # Show the source paths, not the multi-megabyte data URLs.
            extra_body["image"] = [str(p) for p in image_paths]
        _print_request(
            {
                "endpoint": "/v1/images/generations",
                "requested_n": args.n,
                "calls": len(chunks),
                "n_per_call": chunks,
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **sdk_payload,
                **({"extra_body": extra_body} if extra_body else {}),
            }
        )
        return

    print(
        "Calling Image API (generation). This can take up to a couple of minutes.",
        file=sys.stderr,
    )
    client = _create_client()
    sdk_payload, extra_body = _split_extra_body(payload)
    started = time.time()
    written = 0
    for chunk in chunks:
        request = dict(sdk_payload)
        request["n"] = chunk
        if extra_body:
            request["extra_body"] = extra_body
        result = client.images.generate(**request)
        images = _extract_images(result)
        outs = output_paths[written : written + len(images)]
        _decode_write_and_downscale(
            images,
            outs,
            force=args.force,
            downscale_max_dim=args.downscale_max_dim,
            downscale_suffix=args.downscale_suffix,
            output_format=output_format,
        )
        written += len(images)
    elapsed = time.time() - started
    print(f"Generation completed in {elapsed:.1f}s.", file=sys.stderr)


def _edit(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    image_paths = _check_image_paths(args.image)
    mask_path = Path(args.mask) if args.mask else None
    if mask_path:
        if not mask_path.exists():
            _die(f"Mask file not found: {mask_path}")
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Mask exceeds 50MB limit: {mask_path}")

    resolved = _resolve_request(args, has_image=True, input_images=len(image_paths))
    _describe_selection(args, resolved)
    model = resolved["model"]

    if resolved.get("endpoint") == "gemini":
        if mask_path:
            _die(
                "The Gemini endpoint has no mask parameter. Describe the region to change "
                "in --prompt instead, or use a model served over /v1/images/edits."
            )
        _run_gemini(args, resolved, prompt, image_paths, mode="edit")
        return

    if args.size is not None:
        _validate_size(args.size, model)

    payload = {
        "model": model,
        "prompt": prompt,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "input_fidelity": args.input_fidelity,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)
    payload["output_format"] = output_format
    _validate_input_fidelity(args.input_fidelity)
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    chunks = _split_n(args.n, resolved.get("n_cap"))
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [
            str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths
        ]

    if args.dry_run:
        payload_preview = dict(payload)
        payload_preview["n"] = chunks[0]
        payload_preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            payload_preview["mask"] = str(mask_path)
        _print_request(
            {
                "endpoint": "/v1/images/edits",
                "requested_n": args.n,
                "calls": len(chunks),
                "n_per_call": chunks,
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload_preview,
            }
        )
        return

    print(
        f"Calling Image API (edit) with {len(image_paths)} image(s).",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client()
    written = 0
    for chunk in chunks:
        with _open_files(image_paths) as image_files, _open_mask(mask_path) as mask_file:
            request = dict(payload)
            request["n"] = chunk
            request["image"] = image_files if len(image_files) > 1 else image_files[0]
            if mask_file is not None:
                request["mask"] = mask_file
            result = client.images.edit(**request)
        images = _extract_images(result)
        outs = output_paths[written : written + len(images)]
        _decode_write_and_downscale(
            images,
            outs,
            force=args.force,
            downscale_max_dim=args.downscale_max_dim,
            downscale_suffix=args.downscale_suffix,
            output_format=output_format,
        )
        written += len(images)

    elapsed = time.time() - started
    print(f"Edit completed in {elapsed:.1f}s.", file=sys.stderr)


def _open_files(paths: list[Path]):
    return _FileBundle(paths)


def _open_mask(mask_path: Path | None):
    if mask_path is None:
        return _NullContext()
    return _SingleFile(mask_path)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _SingleFile:
    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        self._handle = self._path.open("rb")
        return self._handle

    def __exit__(self, exc_type, exc, tb):
        if self._handle:
            with contextlib.suppress(OSError, ValueError):
                self._handle.close()
        return False


class _FileBundle:
    def __init__(self, paths: list[Path]):
        self._paths = paths
        self._handles: list[BinaryIO] = []

    def __enter__(self):
        self._handles = [p.open("rb") for p in self._paths]
        return self._handles

    def __exit__(self, exc_type, exc, tb):
        for handle in self._handles:
            with contextlib.suppress(OSError, ValueError):
                handle.close()
        return False


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "'auto' (default) picks the cheapest model the API key can reach that "
            "fits the task; pin an id such as seedream-5.0-flash to bypass."
        ),
    )
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument(
        "--task",
        choices=sorted(CATALOG_TASKS),
        default=DEFAULT_TASK,
        help=(
            "What the image is for. Drives model choice. 'auto' infers it: edit when "
            "an input image is involved, otherwise general."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("cheapest", "balanced", "best"),
        default=DEFAULT_PROFILE,
        help=(
            "Cost/quality posture for --model auto. cheapest (default) minimizes CNY; "
            "balanced and best trade money for capability."
        ),
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        help="Refuse to auto-select a model whose estimated cost exceeds this many CNY.",
    )
    parser.add_argument(
        "--thinking",
        choices=("minimal", "high"),
        default=None,
        help=(
            "Thinking budget for models with a thinking knob (gemini-nano-banana-2.1). "
            "'minimal' (the default) emits no thinking tokens; 'high' adds roughly 820 "
            "output tokens, about +CNY 0.13 at 1K. Ignored by every other model."
        ),
    )
    parser.add_argument(
        "--size",
        default=argparse.SUPPRESS,
        help=(
            "WIDTHxHEIGHT (WIDTH*HEIGHT accepted), a keyword such as 2K where the model "
            "supports it, or 'auto' where the model allows it. generate / generate-batch "
            "default: the selected model's own default size. edit default: the parameter "
            "is omitted and the relay chooses the resolution (it preserves the input "
            "aspect ratio but may return different pixel dimensions); 'auto' itself is "
            "not accepted by the edits endpoint."
        ),
    )
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument(
        "--watermark",
        dest="watermark",
        action="store_true",
        default=None,
        help="Keep the relay's default watermark on models that add one (seedream).",
    )
    parser.add_argument(
        "--no-watermark",
        dest="watermark",
        action="store_false",
        help="Explicitly ask for no watermark (this is already the default).",
    )
    parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--out-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    # Prompt augmentation hints
    parser.add_argument("--use-case")
    parser.add_argument("--scene")
    parser.add_argument("--subject")
    parser.add_argument("--style")
    parser.add_argument("--composition")
    parser.add_argument("--lighting")
    parser.add_argument("--palette")
    parser.add_argument("--materials")
    parser.add_argument("--text")
    parser.add_argument("--constraints")
    parser.add_argument("--negative")

    # Post-processing (optional): generate an additional downscaled copy for fast web loading.
    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)


def _list_models(args: argparse.Namespace) -> None:
    """Print the catalog, the price, and whether this key can reach each model."""
    if _catalog is None:
        _die("The model catalog (scripts/model_catalog.py) is missing.")
    if getattr(args, "refresh", False):
        _accessible_models(refresh=True)
    accessible = _accessible_models()
    task = getattr(args, "task", None) or DEFAULT_TASK
    profile = getattr(args, "profile", None) or DEFAULT_PROFILE

    models = list(_catalog.CATALOG.values())  # type: ignore[union-attr]
    if getattr(args, "images_only", False):
        models = [m for m in models if m.endpoint == "images"]
    models.sort(key=lambda m: (m.kind != "image", m.price_cny, -m.tier))

    if getattr(args, "json", False):
        payload = [
            {
                "id": m.id,
                "vendor": m.vendor,
                "kind": m.kind,
                "price_cny": m.price_cny,
                "estimated_cny": _catalog.estimate_cost_cny(m, size=m.size_default or None, n=1),  # type: ignore[union-attr]
                "tier": m.tier,
                "caps": sorted(m.caps),
                "n_max": m.n_max,
                "endpoint": m.endpoint,
                "accessible": (accessible is None or m.id in accessible),
                "latency_s": m.latency_s,
                "note": m.note,
            }
            for m in models
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    for m in models:
        if m.kind == "image":
            price = f"CNY {m.price_cny:.2f}/image"
        elif m.out_per_m:
            where = m.size_default or "1024x1024"
            est = _catalog.estimate_cost_cny(m, size=where, n=1)  # type: ignore[union-attr]
            qual = "" if m.out_tokens_by_tier else " medium"
            price = f"token (~CNY {est:.2f} at {where}{qual})"
        else:
            price = f"token ({m.endpoint} endpoint)"
        reach = "yes" if (accessible is None or m.id in accessible) else "NO (key limit)"
        if accessible is None:
            reach = "unknown (no key)"
        speed = f"~{m.latency_s:.0f}s" if m.latency_s else "unmeasured"
        print(
            f"{m.id:26} {price:34} tier {m.tier}  n<={m.n_max or '-'}  "
            f"{speed:9} reach={reach}"
        )
        print(f"{'':26} caps: {', '.join(sorted(m.caps))}")
        if m.note:
            print(f"{'':26} {m.note}")

    if accessible is not None:
        print(f"\n{len(accessible)} model ids reachable by this key: {', '.join(sorted(accessible))}")
    excluded = getattr(_catalog, "EXCLUDED_MODELS", {})  # type: ignore[union-attr]
    if excluded:
        print("\nDeliberately not offered:")
        for model_id, why in sorted(excluded.items()):
            print(f"  {model_id}: {why}")
    pick, reason = _catalog.choose_model(task, profile=profile, accessible=accessible)  # type: ignore[union-attr]
    print(f"\nFor --task {task} --profile {profile}: {(pick.id if pick else 'none')} ({reason})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fallback CLI for explicit image generation or editing via the Acctoken relay"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    gen_parser.add_argument(
        "--image",
        action="append",
        help=(
            "Reference / input image for generate (repeat for several). Sent as a data "
            "URL in the `image` field. Required by utility models (image-cutout, "
            "image-hd, seedream-5.0-pro-layers) and used as reference guidance by "
            "seedream / qwen / wan."
        ),
    )
    gen_parser.add_argument(
        "--cutout-scene",
        choices=("general", "human", "product"),
        help="image-cutout scene: general (default), human, or product.",
    )
    gen_parser.add_argument(
        "--upscale-multiple",
        type=int,
        choices=(2, 3, 4),
        help="image-hd upscale factor, 2-4 (default 2).",
    )
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch",
        help="Generate multiple prompts concurrently (JSONL input)",
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument(
        "--input", required=True, help="Path to JSONL file (one job per line)"
    )
    batch_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    batch_parser.add_argument("--max-attempts", type=int, default=3)
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.set_defaults(func=_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--input-fidelity")
    edit_parser.set_defaults(func=_edit)

    models_parser = subparsers.add_parser(
        "models",
        help="List the image models, their prices, and whether this API key can reach them",
    )
    models_parser.add_argument(
        "--refresh", action="store_true", help="Re-probe /v1/models instead of using the cache"
    )
    models_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    models_parser.add_argument(
        "--images-only", action="store_true", help="Hide models served over the chat endpoint"
    )
    models_parser.add_argument("--task", default=DEFAULT_TASK)
    models_parser.add_argument(
        "--profile", choices=("cheapest", "balanced", "best"), default=DEFAULT_PROFILE
    )
    models_parser.set_defaults(func=_list_models)

    args = parser.parse_args()

    if args.command == "models":
        args.func(args)
        return 0

    # Resolve the --size default per endpoint (see EDIT_REJECTED_SIZES). With
    # argparse.SUPPRESS the attribute is absent exactly when the flag was not given, so
    # "not specified" never gets sent as `auto` to the edits endpoint. Payload builders
    # drop None values, which is what omits size from an edit request -- note that lets
    # the relay pick the resolution; it does not freeze the input dimensions. For
    # generate / generate-batch an unset size is resolved from the selected model.
    size_explicit = "size" in vars(args)
    if args.command == "edit":
        if not size_explicit:
            args.size = None
        elif _normalize_size_name(args.size) in EDIT_REJECTED_SIZES:
            _die(
                "--size auto is not accepted by the edits endpoint. Omit --size to keep "
                "the input image's dimensions, or pass an explicit size such as "
                "--size 1024x1536."
            )
    elif not size_explicit:
        args.size = None

    if args.n < 1 or args.n > 10:
        _die("--n must be between 1 and 10")
    if getattr(args, "concurrency", 1) < 1 or getattr(args, "concurrency", 1) > 25:
        _die("--concurrency must be between 1 and 25")
    if getattr(args, "max_attempts", 3) < 1 or getattr(args, "max_attempts", 3) > 10:
        _die("--max-attempts must be between 1 and 10")
    if args.max_cost is not None and args.max_cost < 0:
        _die("--max-cost must be >= 0")
    if args.output_compression is not None and not (
        0 <= args.output_compression <= 100
    ):
        _die("--output-compression must be between 0 and 100")
    if args.command == "generate-batch" and not args.out_dir:
        _die("generate-batch requires --out-dir")
    if args.command == "generate" and args.task in ("cutout", "upscale") and not args.image:
        _die(f"--task {args.task} needs --image.")
    if args.command == "edit":
        rejected = [o for o in GENERATE_ONLY_OPTIONS if getattr(args, o, None) is not None]
        if rejected:
            _die(
                "--" + ", --".join(rejected)
                + " is not accepted by the edits endpoint; it applies to generate and "
                "generate-batch only."
            )
    if (
        getattr(args, "downscale_max_dim", None) is not None
        and args.downscale_max_dim < 1
    ):
        _die("--downscale-max-dim must be >= 1")

    _validate_model(args.model)
    if args.size is not None and args.model != "auto":
        _validate_size(args.size, args.model)
    _validate_quality(args.quality)
    _validate_background(args.background)
    _validate_model_specific_options(
        model=args.model,
        input_fidelity=getattr(args, "input_fidelity", None),
    )
    _ensure_api_key(args.dry_run)

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
