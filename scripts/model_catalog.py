"""Capability + price catalog for the Acctoken image relay.

This module is the source of truth for *cost-aware* model selection in
``scripts/image_gen.py``. Prices and descriptions come from the relay's own
pricing endpoint ``GET https://www.acctoken.com/api/pricing`` (the same data the
pricing web page renders), cross-checked against a live probe of
``GET /v1/images/generations`` on 2026-10-17.

Two ideas drive the design:

* **Money is per image, not per token, for the Chinese models.** The whole
  ``seedream`` / ``qwen-image`` / ``wan2.7`` / ``z-image`` / ``image-*`` family is
  billed a flat CNY amount per delivered image, so for them a bigger size or a
  higher ``quality`` is *free*. ``gpt-image-2.5`` and ``gemini-nano-banana-2.1``
  are token-priced (they are routed through upstream providers that bill tokens),
  so for those size + quality change the bill.
* **The cheapest model that can actually do the job is the default.** A plain
  picture is ``z-image-turbo`` (¥0.10) or ``seedream-5.0-flash`` (¥0.12); a
  background cutout is ``image-cutout`` (¥0.02); an upscale is ``image-hd``
  (¥0.06). Expensive flagships (``*-pro``, ``gpt-image-2.5``) are only chosen when
  the task needs them (dense in-image text, 4K, best instruction following) or the
  caller asks for the ``balanced`` / ``best`` profile.
* **Latency is a hard constraint, not a tiebreaker.** Money decides between models
  that can all answer in the same order of time, but a model that takes two minutes
  is unusable at the CLI's default 120 s timeout no matter how cheap it is. Where a
  model's wall-clock time has been measured it is recorded in ``latency_s``; a model
  measured far outside the pack is removed from the catalog outright and listed in
  ``EXCLUDED_MODELS`` (with the reason) so the CLI can explain the absence instead of
  silently passing the id through.

Prices are CNY per image unless ``kind == "token"``. Keep this file in sync with
the live page by running ``scripts/update_pricing.py`` (it rewrites
``references/models.md`` and warns when a price here has drifted).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Capability vocabulary
# --------------------------------------------------------------------------- #
# t2i        - text to image
# edit       - takes an input image and changes it
# ref        - accepts extra reference images (style / subject guidance)
# text       - reliably renders in-image text (including CJK)
# transparent- can emit alpha / transparent PNG
# cutout     - background removal only
# upscale    - super resolution / upscaling
# layers     - layer decomposition (image -> editable layers)
# fourk      - can output 4K-class images

TIER_DRAFT = 1
TIER_STANDARD = 2
TIER_HIGH = 3
TIER_FLAGSHIP = 4


@dataclass(frozen=True)
class Model:
    id: str
    vendor: str
    kind: str  # "image" (per delivered image) or "token"
    price_cny: float = 0.0  # per image, for kind == "image"
    official_cny: float = 0.0  # vendor list price, for showing the discount
    tier: int = TIER_STANDARD
    caps: frozenset = frozenset()
    endpoint: str = "images"  # "images" | "chat"

    # Request-shape constraints
    n_max: int | None = None  # None -> let the relay decide
    needs_image: bool = False  # an input image is required (cutout/upscale/layers)
    size_min_edge: int | None = None
    size_max_edge: int | None = None
    size_min_pixels: int | None = None
    size_max_pixels: int | None = None
    size_multiple_of: int | None = None
    size_max_ratio: float | None = None
    size_keywords: tuple = ()  # literal "2K" / "4K" style sizes the relay accepts
    size_default: str = "1024x1024"
    size_4k: str = ""  # default when the task explicitly asks for 4K-class output
    # Whether the relay accepts size="auto". Verified False for the seedream family
    # (the relay answers "size 'auto' is only supported with layer_decomposition");
    # left True elsewhere so a valid `auto` is not rejected locally.
    accepts_auto_size: bool = True
    quality: bool = False
    quality_affects_price: bool = False
    watermark: bool = False  # accepts watermark=false
    watermark_default_on: bool = False
    response_format: bool = True
    extra_params: tuple = ()  # e.g. ("scene", "multiple")

    groups: tuple = ("default",)
    input_max_mb: float | None = None
    note: str = ""
    # Measured wall-clock seconds for one 1024-class image, when it has been timed.
    # None means "not measured" (unknown speed), not "fast".
    latency_s: float | None = None

    # Token pricing (kind == "token")
    in_text_per_m: float = 0.0
    in_image_per_m: float = 0.0
    out_per_m: float = 0.0
    out_tokens_1k: tuple = ()  # (low, medium, high) output tokens at 1024x1024
    out_tokens_by_tier: tuple = ()  # (("1K", 1120), ("2K", 1680), ...) for "gemini"
    ref_image_tokens: int = 1100  # input tokens billed per reference image

    # Gemini-native request shape (endpoint == "gemini"): size is expressed as an
    # imageSize tier plus an aspectRatio, not a WIDTHxHEIGHT pair.
    image_tiers: tuple = ()  # accepted imageSize values, cheapest first
    aspect_ratios: tuple = ()  # accepted aspectRatio values
    thinking: bool = False  # accepts a thinkingLevel knob


# Endpoints this CLI knows how to drive. Both are implemented in image_gen.py;
# "gemini" means the model is only served over POST
# /v1beta/models/<id>:generateContent and rejects /v1/images/generations.
SUPPORTED_ENDPOINTS = frozenset({"images", "gemini"})

# Aspect ratios the Gemini image config accepts, in the order the vendor documents.
GEMINI_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")

# Token-priced models on the relay are billed as
#   cost_CNY = (prompt_tokens + completion_tokens * completion_ratio) * model_ratio / 500000
# where model_ratio / completion_ratio come from GET /api/pricing. Calibrated against
# gpt-image-2.5, whose published rates (¥56/M text in, ¥89.6/M image in, ¥336/M out)
# reproduce exactly from model_ratio=28, image_ratio=1.6, completion_ratio=6 with a
# factor of 2 CNY per million per ratio unit -- and whose documented per-image prices
# (¥0.15 at 1024x1024 medium, ¥0.59 high, ¥1.2 at 2048x2048 high) then fall out of this
# same formula. The ratio -> CNY factor is the one free constant; everything else is
# the vendor's own data.
RATIO_TO_CNY_PER_M = 2.0
QUOTA_PER_CNY = 500_000.0

_S = frozenset

CATALOG: dict[str, Model] = {
    # ---- seedream (ByteDance / Volcengine, flat per image) ---------------- #
    "seedream-5.0-flash": Model(
        id="seedream-5.0-flash",
        vendor="volcengine",
        kind="image",
        price_cny=0.12,
        official_cny=0.12,
        tier=TIER_STANDARD,
        caps=_S({"t2i", "edit", "ref"}),
        n_max=1,
        size_min_pixels=921_600,
        size_max_edge=4096,
        size_max_ratio=4.0,
        size_keywords=("2K",),
        size_default="1024x1024",
        accepts_auto_size=False,
        watermark=True,
        watermark_default_on=True,
        note="Fastest seedream. n=1 only. size must be '2K' or WxH with >=921,600 px. "
        "Watermark is on unless watermark=false. Reference-image input is free.",
        latency_s=16.0,
    ),
    "seedream-5.0-pro": Model(
        id="seedream-5.0-pro",
        vendor="volcengine",
        kind="image",
        price_cny=0.54,
        official_cny=0.60,
        tier=TIER_FLAGSHIP,
        caps=_S({"t2i", "edit", "ref", "fourk"}),
        n_max=1,
        size_min_pixels=921_600,
        size_max_edge=4096,
        size_max_ratio=4.0,
        size_keywords=("2K",),
        size_default="2048x2048",
        size_4k="2048x2048",
        accepts_auto_size=False,
        watermark=True,
        watermark_default_on=True,
        note="2K flagship, billed flat at the 2K tier even for smaller sizes. "
        "Up to 4 free reference images. n=1 only.",
    ),
    "seedream-5.0-pro-layers": Model(
        id="seedream-5.0-pro-layers",
        vendor="volcengine",
        kind="image",
        price_cny=0.15,
        official_cny=0.15,
        tier=TIER_HIGH,
        caps=_S({"edit", "layers", "transparent"}),
        endpoint="images",
        n_max=1,
        needs_image=True,
        size_keywords=("1K", "1.5K", "2K"),
        size_default="1.5K",
        note="Layer decomposition: 1 base image + up to 16 transparent PNG layers. "
        "Billed per delivered layer (base counts as one), so ~¥1.2-2 per call.",
    ),

    # ---- qwen-image (Alibaba / Bailian, flat per image) ------------------- #
    # The whole qwen-image-3.x line was dropped from this catalog on 2026-10-17 for
    # latency. qwen-image-3.0 was measured at 123.3 s for one 1024x1536 image, against
    # 13-19 s for seedream-5.0-flash and 15.4 s for gemini-nano-banana-2.1, so it blew
    # the CLI's 120 s timeout; qwen-image-3.0-pro sits on the same Bailian upstream and
    # is documented at ~5 requests/minute, so it was removed on the same reasoning even
    # though it was never timed directly. For in-image text use gemini-nano-banana-2.1 or
    # gpt-image-2.5. See EXCLUDED_MODELS below.
    "qwen-image-2.0": Model(
        id="qwen-image-2.0",
        vendor="ali",
        kind="image",
        price_cny=0.15,
        official_cny=0.20,
        tier=TIER_STANDARD,
        caps=_S({"t2i", "edit", "ref", "text"}),
        n_max=6,
        size_min_edge=512,
        size_max_edge=2048,
        size_default="1024x1024",
        note="Accelerated 2.0; 512-2048 px, up to 6 images per call. Good value text.",
    ),
    "qwen-image-2.0-pro": Model(
        id="qwen-image-2.0-pro",
        vendor="ali",
        kind="image",
        price_cny=0.38,
        official_cny=0.50,
        tier=TIER_HIGH,
        caps=_S({"t2i", "edit", "ref", "text"}),
        n_max=6,
        size_min_edge=512,
        size_max_edge=2048,
        note="Full-strength 2.0; posters and layouts. Rate-limited (~2 req/min).",
    ),

    # ---- wan2.7-image (Alibaba Wanxiang, flat per image) ------------------ #
    "wan2.7-image": Model(
        id="wan2.7-image",
        vendor="ali",
        kind="image",
        price_cny=0.15,
        official_cny=0.20,
        tier=TIER_HIGH,
        caps=_S({"t2i", "edit", "ref"}),
        n_max=4,
        size_keywords=("1K", "2K"),
        size_default="1K",
        note="Instruction-style edit + generation; up to 4 images per call.",
    ),
    "wan2.7-image-pro": Model(
        id="wan2.7-image-pro",
        vendor="ali",
        kind="image",
        price_cny=0.38,
        official_cny=0.50,
        tier=TIER_FLAGSHIP,
        caps=_S({"t2i", "edit", "ref", "fourk"}),
        n_max=4,
        size_keywords=("1K", "2K", "4K"),
        size_default="2K",
        size_4k="4K",
        note="Better instruction following; 4K generation, 2K edits, up to 4 images.",
    ),

    # ---- z-image (Alibaba, flat per image) ------------------------------- #
    "z-image-turbo": Model(
        id="z-image-turbo",
        vendor="ali",
        kind="image",
        price_cny=0.10,
        official_cny=0.10,
        tier=TIER_STANDARD,
        caps=_S({"t2i", "text"}),
        n_max=None,
        size_min_edge=512,
        size_max_edge=2048,
        size_default="1024x1536",
        note="Cheapest plain text-to-image (¥0.10). 8-step; prompt_extend doubles to ¥0.20.",
    ),

    # ---- utility models (flat per image) ---------------------------------- #
    "image-cutout": Model(
        id="image-cutout",
        vendor="volcengine",
        kind="image",
        price_cny=0.02,
        official_cny=0.02,
        tier=TIER_STANDARD,
        caps=_S({"edit", "cutout", "transparent"}),
        n_max=1,
        needs_image=True,
        size_default="",
        response_format=True,
        extra_params=("scene",),
        input_max_mb=30.0,
        note="Background removal -> transparent PNG/WebP. scene=general|human|product. "
        "Cheapest way to a transparent asset (¥0.02).",
    ),
    "image-hd": Model(
        id="image-hd",
        vendor="volcengine",
        kind="image",
        price_cny=0.06,
        official_cny=0.06,
        tier=TIER_STANDARD,
        caps=_S({"edit", "upscale"}),
        n_max=1,
        needs_image=True,
        size_default="",
        extra_params=("multiple",),
        input_max_mb=10.0,
        note="2-4x upscale that preserves identity/text/texture. Input long edge <=2048.",
    ),

    # ---- OpenAI gpt-image-2.5 (token priced) ------------------------------ #
    "gpt-image-2.5": Model(
        id="gpt-image-2.5",
        vendor="openai",
        kind="token",
        tier=TIER_FLAGSHIP,
        caps=_S({"t2i", "edit", "ref", "text", "transparent", "fourk"}),
        n_max=1,
        size_multiple_of=16,
        size_min_pixels=655_360,
        size_max_pixels=8_294_400,
        size_max_edge=3840,
        size_max_ratio=3.0,
        size_default="1024x1024",
        size_4k="3840x2160",
        quality=True,
        quality_affects_price=True,
        response_format=False,
        in_text_per_m=56.0,
        in_image_per_m=89.6,
        out_per_m=336.0,
        out_tokens_1k=(140, 440, 1730),
        note="Best English+Chinese in-image text and instruction following, but token-priced "
        "(~¥0.15 at 1024x1024 medium, ~¥0.59 high, ~¥1.2 at 2048x2048 high). n=1 only; "
        "no response_format param.",
    ),

    # ---- Gemini nano-banana (token priced, Gemini native endpoint) --------- #
    # Everything below is measured against the live relay on 2026-10-17, not copied
    # from the pricing blurb: the blurb points at /v1/chat/completions with
    # extra_body.google.image_config, but that path is a dead end -- it silently
    # ignores aspect_ratio and returns 1376x768 for every request. The Gemini native
    # endpoint honours imageConfig and, with thinking_level=minimal, emits no
    # thinking tokens at all.
    "gemini-nano-banana-2.1": Model(
        id="gemini-nano-banana-2.1",
        vendor="google",
        kind="token",
        tier=TIER_FLAGSHIP,
        caps=_S({"t2i", "edit", "ref", "text", "fourk"}),
        endpoint="gemini",
        n_max=1,
        image_tiers=("1K", "2K", "4K"),
        aspect_ratios=GEMINI_ASPECT_RATIOS,
        thinking=True,
        size_keywords=("1K", "2K", "4K"),
        accepts_auto_size=False,
        size_default="1K",
        size_4k="4K",
        response_format=False,
        in_text_per_m=7.96,
        in_image_per_m=7.96,
        out_per_m=159.23,
        out_tokens_by_tier=(("1K", 1120), ("2K", 1680), ("4K", 3780)),
        ref_image_tokens=1120,
        note="Cheapest flagship (tier 4): ~¥0.18 at 1K, ~¥0.27 at 2K, ~¥0.60 at 4K, and the "
        "fastest of the token-priced models (~15s). Best at instruction-following edits and "
        "fusing several reference images; solid Chinese+English in-image text. Served over "
        "POST /v1beta/models/gemini-nano-banana-2.1:generateContent, NOT "
        "/v1/images/generations. Size is aspectRatio + imageSize (1K/2K/4K), not WxH; "
        "thinking_level=minimal (the default here) avoids ~820 extra thinking tokens. "
        "n=1 only. Every output carries a Google C2PA credential and a SynthID watermark "
        "that cannot be switched off; the model also returns a low-res draft image before "
        "the final one, and this CLI keeps only the final image.",
        latency_s=15.4,
    ),
}


# Model ids the relay sells but this catalog refuses to offer, with the reason. The CLI
# uses this to fail loudly on `--model <id>` instead of letting an id the catalog no
# longer prices fall through to the "unknown model, pass it through" path -- which is
# exactly the path that used to hand a two-minute model to a 120 s timeout.
EXCLUDED_MODELS: dict[str, str] = {
    "qwen-image-3.0": (
        "removed for latency: measured 123.3 s per 1024x1536 image on 2026-10-17, "
        "against 13-19 s for seedream-5.0-flash and 15.4 s for gemini-nano-banana-2.1. "
        "For in-image text use gemini-nano-banana-2.1 (1K ~CNY 0.18) or gpt-image-2.5"
    ),
    "qwen-image-3.0-pro": (
        "removed with the rest of the qwen-image-3.x line: same Bailian upstream as "
        "qwen-image-3.0 (measured 123.3 s per image) and rate-limited to about 5 "
        "requests/minute. It was never timed directly -- the removal is on the shared "
        "upstream, not on its own measurement. For posters and CJK text use "
        "gemini-nano-banana-2.1 or gpt-image-2.5"
    ),
}


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #
def _parse_wh(size: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"([1-9][0-9]*)\s*[xX*]\s*([1-9][0-9]*)", str(size).strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _keyword_pixels(keyword: str) -> int:
    """Rough pixel count for a '1K'/'2K'/'4K' style size (square, 1K=1024)."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)[kK]", keyword.strip())
    if not m:
        return 1024 * 1024
    edge = float(m.group(1)) * 1024
    return int(edge * edge)


def aspect_ratio_for(width: int, height: int, allowed: tuple = GEMINI_ASPECT_RATIOS) -> str:
    """Closest supported aspectRatio string for a WIDTHxHEIGHT request.

    Gemini takes a ratio plus a size tier rather than pixel dimensions, so a caller
    asking for 2048x1152 has to be mapped onto 16:9 + 2K. Ties go to the earlier
    entry, which keeps the documented order (1:1 first) as the tie-breaker.
    """
    if width <= 0 or height <= 0:
        return allowed[0] if allowed else "1:1"
    target = width / height
    best, best_err = allowed[0], float("inf")
    for name in allowed:
        w_s, _, h_s = name.partition(":")
        try:
            err = abs(target - (int(w_s) / int(h_s)))
        except (ValueError, ZeroDivisionError):
            continue
        if err < best_err - 1e-9:
            best, best_err = name, err
    return best


def image_tier_for(width: int, height: int, tiers: tuple) -> str:
    """Cheapest imageSize tier (1K/2K/4K) that covers a WIDTHxHEIGHT request."""
    if not tiers:
        return "1K"
    edge = max(width, height)
    if edge <= 1536:
        want = "1K"
    elif edge <= 3072:
        want = "2K"
    else:
        want = "4K"
    for name in tiers:
        if name.upper() == want:
            return name
    return tiers[-1]


def _tier_tokens(model: "Model", size: str | None) -> tuple[str, int] | None:
    """Resolve a requested size to (tier, output tokens) for a tier-priced model.

    ``gemini-nano-banana-2.1`` is billed a flat number of image tokens per tier
    (1K=1120, 2K=1680, 4K=3780) rather than by area, so the sqrt scaling used for
    gpt-image-2.5 would be wrong for it. Returns None when the model has no tiers.
    """
    tiers: dict[str, int] = dict(model.out_tokens_by_tier)  # type: ignore[arg-type]
    if not tiers:
        return None

    def _pick(name: str) -> tuple[str, int] | None:
        for key in tiers:
            if key.upper() == name.upper():
                return key, tiers[key]
        return None

    first = next(iter(tiers))
    if not size:
        return first, tiers[first]

    raw = str(size).strip()
    exact = _pick(raw)
    if exact is not None:
        return exact

    parsed = _parse_wh(raw)
    if parsed is None:
        # An unrecognised keyword: fall back to the cheapest tier rather than guess high.
        return first, tiers[first]

    want = image_tier_for(parsed[0], parsed[1], tuple(tiers))
    return _pick(want) or (first, tiers[first])


def estimate_cost_cny(
    model: "Model",
    *,
    size: str | None = None,
    quality: str | None = None,
    n: int = 1,
    input_images: int = 0,
    thinking_tokens: int = 0,
) -> float:
    """Best-effort CNY cost for one request. Never raises; unknown -> cheap.

    Per-image models are exact (flat CNY per delivered image). Token models are
    estimated from the vendor's documented token counts: tier-priced models use the
    tier table, area-priced models scale a 1024x1024 baseline by pixel count. The
    estimate rounds *down* when a value is unknown, because the caller ranks
    alternatives cheapest-first -- an over-estimate would push work onto a model
    that only looks cheaper.
    """
    n = max(1, int(n))
    if model.kind == "image":
        return round(model.price_cny * n, 4)

    tiered = _tier_tokens(model, size)
    if tiered is not None:
        _, base = tiered
        out_tokens = (base + max(0, thinking_tokens)) * n
    else:
        # Token model: use the documented output-token baseline, scale sub-linearly.
        low, medium, high = (model.out_tokens_1k or (150, 450, 1700))
        q = (quality or "medium").lower()
        base = {"low": low, "medium": medium, "high": high, "auto": medium}.get(q, medium)
        pixels = 1024 * 1024
        if size and str(size).lower() != "auto":
            parsed = _parse_wh(size)
            pixels = parsed[0] * parsed[1] if parsed else _keyword_pixels(size)
        scale = (pixels / (1024 * 1024)) ** 0.5
        out_tokens = base * scale * n + max(0, thinking_tokens)

    cost = out_tokens * model.out_per_m / 1_000_000
    # Reference images are billed as input tokens (1100-1120 each is the documented
    # figure). Small next to the image output, but include it for edits.
    if input_images:
        cost += input_images * model.ref_image_tokens * model.in_image_per_m / 1_000_000
    cost += 40 * model.in_text_per_m / 1_000_000
    return round(cost, 4)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
# Task -> capability requirements and a human label.
TASK_CAPS: dict[str, frozenset] = {
    "draft": frozenset({"t2i"}),
    "general": frozenset({"t2i"}),
    "photo": frozenset({"t2i"}),
    "illustration": frozenset({"t2i"}),
    "text": frozenset({"t2i", "text"}),
    "edit": frozenset({"edit"}),
    "cutout": frozenset({"cutout"}),
    "upscale": frozenset({"upscale"}),
    "layers": frozenset({"layers"}),
    "4k": frozenset({"t2i", "fourk"}),
}

PROFILE_TIER = {"cheapest": TIER_DRAFT, "balanced": TIER_HIGH, "best": TIER_FLAGSHIP}


def default_size_for(model: Model, task: str) -> str | None:
    """The size a model would pick for a task when the caller passes no --size.

    Shared with the CLI so the resolver prices a candidate at the size the run
    will actually use. Without this, `--task 4k` would compare models at their
    1024x1024 default and pick one that is cheapest on paper but expensive at 4K.
    """
    if task == "4k" and model.size_4k:
        return model.size_4k
    return model.size_default or None


def candidates_for(
    task: str,
    *,
    n: int = 1,
    transparent: bool = False,
    needs_image: bool = False,
    accessible: set[str] | None = None,
) -> list[Model]:
    """Models that can satisfy the request, cheapest first (by list price).

    `n` is accepted for readability but is deliberately *not* used to filter:
    the CLI fans a request for several images out into several single-image calls,
    so a model capped at n=1 can still serve n=4 and may be far cheaper than one
    that emits four images per call. Cost (which scales with n) decides.
    """
    wanted = set(TASK_CAPS.get(task, TASK_CAPS["general"]))
    if transparent:
        wanted.add("transparent")
    out: list[Model] = []
    for model in CATALOG.values():
        if model.endpoint not in SUPPORTED_ENDPOINTS:
            continue
        if accessible is not None and model.id not in accessible:
            continue
        if not wanted <= set(model.caps):
            continue
        if needs_image and not (model.needs_image or model.caps & {"edit", "ref"}):
            continue
        out.append(model)
    out.sort(key=lambda m: (m.price_cny if m.kind == "image" else 999.0, -m.tier))
    return out


def choose_model(
    task: str,
    *,
    profile: str = "cheapest",
    n: int = 1,
    transparent: bool = False,
    needs_image: bool = False,
    accessible: set[str] | None = None,
    max_cost_cny: float | None = None,
    size: str | None = None,
    quality: str | None = None,
    input_images: int = 0,
    thinking_tokens: int = 0,
    pinned_model: str | None = None,
) -> tuple[Model | None, str]:
    """Pick a model and explain why. Returns (model, reason). model=None -> none fits."""
    if pinned_model and pinned_model != "auto":
        model = CATALOG.get(pinned_model)
        if model is None:
            return None, f"{pinned_model!r} is not in the catalog; pass it to the relay unchanged"
        return model, f"model pinned explicitly ({model.vendor}, {model.kind})"

    pool = candidates_for(
        task, n=n, transparent=transparent, needs_image=needs_image, accessible=accessible
    )
    if not pool:
        return None, f"no accessible model can do task={task!r}"

    min_tier = PROFILE_TIER.get(profile, TIER_DRAFT)
    if profile == "cheapest":
        considered = pool
    else:
        considered = [m for m in pool if m.tier >= min_tier] or pool

    # A balanced/best profile implies quality, so price a token model at `high`
    # rather than the cheap `medium` default. Otherwise gpt-image-2.5 would win
    # every balanced tie on the strength of its cheapest (lowest-quality) tier.
    cost_quality = "high" if profile in ("balanced", "best") else quality

    def cost(m: Model) -> float:
        return estimate_cost_cny(
            m,
            size=size or default_size_for(m, task),
            quality=cost_quality,
            n=n,
            input_images=input_images,
            thinking_tokens=thinking_tokens if m.thinking else 0,
        )

    if profile == "best":
        # highest tier, then cheapest within it
        best_tier = max(m.tier for m in considered)
        considered = [m for m in considered if m.tier == best_tier]
        pick = min(considered, key=cost)
    else:
        pick = min(considered, key=lambda m: (cost(m), -m.tier))

    if max_cost_cny is not None and cost(pick) > max_cost_cny:
        return None, (
            f"the cheapest model for task={task!r} is {pick.id} "
            f"(~{cost(pick):.2f} CNY), which exceeds --max-cost {max_cost_cny:.2f}"
        )

    reason = (
        f"task={task} profile={profile} -> {pick.id} "
        f"({pick.vendor}, ~{cost(pick):.2f} CNY)"
    )
    return pick, reason
