# CLI reference (`scripts/run.sh` -> `scripts/image_gen.py`)

This skill generates and edits images through the **Acctoken** OpenAI-compatible API
relay. Read this file when you need the exact CLI surface, defaults, or flags.

## What this CLI does
- `generate`: generate a new image from a prompt (optionally with reference / input images)
- `edit`: edit one or more existing images
- `generate-batch`: run many generation jobs from a JSONL file
- `models`: list the catalog, prices, and which models this API key can reach

Real API calls require **network access**. `--dry-run` does not (it still probes
`/v1/models` unless `ACCTOKEN_IMAGEGEN_OFFLINE=1` is set).

## Configuration
- Base URL: `https://www.acctoken.com/v1` (OpenAI-compatible)
- Model: `auto` — a cost-aware resolver picks the cheapest model the key can reach that
  fits the request. Pin an id to bypass it. See `references/models.md`.
- Dependencies: `openai`, `pillow`, `httpx` — resolved by `scripts/run.sh`, never assumed
- API key resolution, first match wins:
  1. `ACCTOKEN_API_KEY` environment variable
  2. `IMAGE_API_KEY` environment variable
  3. `OPENAI_API_KEY` environment variable
  4. `ACCTOKEN_IMAGE_ENV` file (explicit override, disables the list below)
  5. `$XDG_CONFIG_HOME/acctoken-imagegen.env`, defaulting to `~/.config/acctoken-imagegen.env`,
     or the XDG-style `$XDG_CONFIG_HOME/acctoken-imagegen/env`
  6. `~/acctoken-imagegen.env`, or `~/acctoken-imagegen/env`
  7. Legacy per-agent files: `~/.codex/`, `~/.pi/agent/`, `~/.claude/acctoken-imagegen.env`

  Inside those files only `ACCTOKEN_API_KEY`, `IMAGE_API_KEY` and `OPENAI_API_KEY`
  assignments are read.

The key is not stored in the skill scripts, and the skill directory never contains it, so
the directory is safe to copy, sync, or publish. Keep the config file at `chmod 600`.

To set or override the key:

```bash
export ACCTOKEN_API_KEY="sk-..."
# or point at a file anywhere
export ACCTOKEN_IMAGE_ENV=/secure/path/acctoken-imagegen.env
```

## Quick start (works from any repo, any agent)

`scripts/run.sh` is a thin wrapper: it locates a Python interpreter that can import the
dependencies, then runs `image_gen.py`. The interpreter search order and its overrides
(`ACCTOKEN_IMAGEGEN_PYTHON`, `ACCTOKEN_IMAGEGEN_VENV`, `ACCTOKEN_IMAGEGEN_HOME`,
`ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP`) are documented in `scripts/_resolve_python.sh`.

Resolve the path from wherever the skill actually lives — do not hardcode an agent home:

```bash
SKILL_DIR="$HOME/.agents/skills/acctoken-imagegen"   # or <agent skills root>/acctoken-imagegen
export IMAGE_GEN="$SKILL_DIR/scripts/run.sh"
```

To recreate or pre-build the shared environment (also what the self-heal step does):

```bash
uv venv --relocatable "$HOME/.agents/venvs/acctoken-imagegen"
uv pip install --python "$HOME/.agents/venvs/acctoken-imagegen/bin/python" openai pillow httpx
```

List the catalog and what your key can reach:

```bash
bash "$IMAGE_GEN" models            # human table
bash "$IMAGE_GEN" models --json     # machine-readable
```

Dry-run (no API call; prints the resolved model and the exact payload):

```bash
bash "$IMAGE_GEN" generate --prompt "Test" --out output/imagegen/test.png --dry-run
```

Generate (requires network):

```bash
bash "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --out output/imagegen/alpine-cabin.png
```

Edit:

```bash
bash "$IMAGE_GEN" edit \
  --image input.png \
  --prompt "Replace only the background with a warm sunset" \
  --out output/imagegen/sunset-edit.png
```

## Guardrails
- Use the bundled CLI directly (`bash "$IMAGE_GEN" ...`).
- Do **not** create one-off runners unless the user explicitly asks for a custom wrapper.
- Do not silently change the base URL. Changing the model is fine when it is what the
  user asked for; otherwise let the resolver choose.
- Keep the skill directory free of machine-local state (no venv, no key). Per-host state
  belongs in `${ACCTOKEN_IMAGEGEN_HOME:-$HOME/.agents}/venvs/` and `~/.config/`.

## Defaults
- Model: `auto` (resolver; `--profile cheapest`)
- Task: `auto` (infers `edit` when an input image is involved, else `general`)
- Size: on `generate`/`generate-batch`, the selected model's own default (usually
  `1024x1024`). On `edit`, `--size` is **omitted** from the request unless you pass it.
- Quality: `medium`
- Output format: `png`
- Watermark: off by default on models that add one (seedream); pass `--watermark` to keep it.
- Default one-off output path: `output/imagegen/output.png`
- Background: unspecified unless `--background` is set

## Cost-aware model selection

`--model` defaults to `auto`. The resolver intersects the price catalog
(`scripts/model_catalog.py`, regenerated from the relay's `/api/pricing`) with the model
ids the API key can actually reach (`GET /v1/models`), keeps the models whose capabilities
satisfy the request, and picks the cheapest (`--profile cheapest`, the default).

- `--task draft|general|photo|illustration|text|edit|cutout|upscale|layers|4k` narrows the
  capability requirement. `text` needs real in-image text rendering; `cutout` needs
  background removal; `upscale` needs super-resolution; `4k` needs a 4K-class model.
- `--profile balanced` requires tier ≥ high; `--profile best` takes the highest tier, then
  the cheapest within it. Balanced/best price token models at `high` quality so
  `gpt-image-2.5` does not win on its cheapest tier alone.
- `--max-cost 0.5` fails the selection if the cheapest capable model would exceed that
  many CNY.
- `--thinking minimal|high` sets the thinking budget for models that have one
  (`gemini-nano-banana-2.1`). `minimal` (default) emits no thinking tokens; `high` adds
  about 820, roughly +¥0.13 at 1K. Ignored by every other model.
- Pin `--model <id>` to skip the resolver. An id the catalog does not know is passed
  through with a warning.

Cost notes:
- Chinese models (`seedream`, `qwen-image`, `wan2.7`, `z-image`, `image-cutout`,
  `image-hd`) bill a **flat CNY per delivered image**, so size and `quality` are free for
  them.
- `gpt-image-2.5` bills **per token** (¥56/M text in, ¥89.6/M image in, ¥336/M out); the
  estimate is ~¥0.15 at `1024x1024` medium, ~¥0.59 high, ~¥1.2 at `2048x2048` high.
  It is area-priced, so a bigger size costs more.
- `gemini-nano-banana-2.1` bills **per token** too (¥7.96/M in, ¥159.23/M out) but by
  resolution **tier**: ~¥0.18 at `1K`, ~¥0.27 at `2K`, ~¥0.60 at `4K`, plus ~¥0.009 per
  reference image. That makes it the cheapest tier-4 (flagship) model, so it wins
  `--profile balanced` and `--profile best` at 1K/2K — while the flat ¥0.12
  `seedream-5.0-flash` still owns the plain `--profile cheapest` default, and
  `gpt-image-2.5` still owns `--task 4k --profile cheapest`.
- `--n` multiplies the cost. Models capped at one image per call are fanned out into `n`
  calls, so `--n 4 --model seedream-5.0-flash` costs ~¥0.48 and issues four requests.

Refresh the price snapshot (no key needed, hits the public `/api/pricing`):

```bash
bash "$SKILL_DIR/scripts/update_pricing.sh"
```

Refresh the **key's** reachable-model list (cached 6 hours):

```bash
bash "$SKILL_DIR/scripts/run.sh" models --refresh
```

## Per-model constraints the CLI enforces locally

- `seedream-5.0-flash` / `seedream-5.0-pro`: `n` is always 1 (the CLI fans out);
  `size` must be `2K` or a `WIDTHxHEIGHT` with **≥ 921,600 total pixels**; watermark on
  unless `--no-watermark`; up to 4 free reference images (`--image`).
- `seedream-5.0-pro-layers`: layer decomposition; needs one input image; `size`
  `1K`/`1.5K`/`2K`/`auto`; billed per delivered layer.
- `gpt-image-2.5`: multiples of 16, max edge ≤ 3840, ratio ≤ 3:1, 655,360–8,294,400
  total pixels, `n=1` (fanned out); `--input-fidelity` is rejected because inputs always
  use high fidelity; no `response_format` parameter.
- `gemini-nano-banana-2.1`: served over `/v1beta/models/…:generateContent`, **not**
  `/v1/images/generations`. `size` is a tier (`1K`/`2K`/`4K`) or a `WIDTHxHEIGHT` that the
  CLI maps to the closest aspect ratio + tier; `auto` is rejected. `n=1` (fanned out). No
  `quality`, `background`, `output_format`, `moderation`, `watermark` or `--mask` — the CLI
  warns (and for `--mask`, fails) rather than sending them. Always PNG, always carries a
  SynthID watermark and C2PA credential, and always returns a low-res draft before the
  final image (the CLI keeps only the final one). Reference images are unlimited in
  practice and can be fused in one request.
- `qwen-image-2.0` / `-2.0-pro`: `size` 512–2048, up to 6 images per call.
- `image-cutout`: needs one input image, `n=1`, `--cutout-scene general|human|product`.
- `image-hd`: needs one input image, `n=1`, `--upscale-multiple 2|3|4`, input long edge
  ≤ 2048.

## Sizes

`--size` accepts `WIDTHxHEIGHT` (`WIDTH*HEIGHT` also works), a keyword the model allows
(`2K`, `4K`, …), or `auto` where the model allows it.

- `auto` is **not** accepted by the seedream family, by the edits endpoint, or by the
  Gemini endpoint. It is only valid for layer decomposition (`seedream-5.0-pro-layers`).
- seedream needs ≥ 921,600 total pixels: `1024x1024` is fine, `512x512` is rejected.
- `gpt-image-2.5` needs both edges multiples of 16, max edge ≤ 3840, ratio ≤ 3:1, and
  655,360–8,294,400 total pixels.
- `gemini-nano-banana-2.1` accepts a tier (`1K`/`2K`/`4K`), a bare aspect ratio
  (`16:9`, `9:16`, `1:1`, `4:3`, …), the compound `tier@ratio` form (`2K@16:9`), or a
  `WIDTHxHEIGHT`. A `WIDTHxHEIGHT` is snapped to the nearest supported aspect ratio and to
  the tier covering its long edge (≤1536 → 1K, ≤3072 → 2K, else 4K); a bare ratio uses the
  model's default tier (1K). The picture that comes back is the tier and ratio, not your
  exact pixel dimensions.
  - `--size 2K` → 2K at 1:1 · `--size 16:9` → 1K at 16:9 · `--size 2K@16:9` → 2K at 16:9
  - `--size 2560x1440` → snapped to 2K at 16:9
- Open models (other catalog ids): each edge 256–4096 px, ratio ≤ 4:1.

Popular sizes:

- `1024x1024`
- `1536x1024`
- `1024x1536`
- `2048x2048`
- `2048x1152`
- `3840x2160`
- `2160x3840`

### `edit` size behavior

- **`--size auto` is refused on `edit`** (the endpoint rejects it) and fails locally with
  a hint rather than burning a request.
- **Leaving `--size` off an `edit` does not pin the output to the input's dimensions.** The
  parameter is omitted and the relay chooses the resolution: aspect ratio is preserved,
  pixel dimensions may change. Measured on 2026-10-11 with `seedream-5.0-flash`: a
  `1024x1536` input returned as `1664x2496`. When an asset must land on exact dimensions,
  pass `--size` explicitly (and check the result with `Image.open(...).size`).

Fast draft:

```bash
bash "$IMAGE_GEN" generate \
  --prompt "A product thumbnail of a matte ceramic mug on a stone surface" \
  --task draft --size 1024x1024 \
  --out output/imagegen/mug-draft.png
```

Final 2K landscape:

```bash
bash "$IMAGE_GEN" generate \
  --prompt "A polished landing-page hero image of a matte ceramic mug on a stone surface" \
  --task photo --profile balanced --size 2048x1152 \
  --out output/imagegen/mug-hero.png
```

## Quality, input fidelity, and masks

- `--quality` works for `generate`, `edit`, and `generate-batch`: `low|medium|high|auto`
  (only `gpt-image-2.5` prices on it; the flat-priced models and the Gemini endpoint
  ignore it).
- `--thinking` is for models with a thinking knob (`gemini-nano-banana-2.1`);
  `minimal|high`, default `minimal`.
- `--moderation` is **generate / generate-batch only**. The edits endpoint does not accept
  it, so `--moderation` with `edit` fails with a clear message instead of an SDK
  `TypeError`.
- `--input-fidelity` is **edit-only** and validated as `low|high`; it is rejected locally
  for the `gpt-image-2.x` and `qwen-image-` families
- `--mask` is **edit-only**
- `--image` is **required on `edit`** and optional on `generate` (reference / utility input)
- `--cutout-scene` and `--upscale-multiple` are **generate-only**, for `image-cutout` and
  `image-hd` respectively

Mask notes:
- For multi-image edits, pass repeated `--image` flags. Their order is meaningful, so
  describe each image by index and role in the prompt.
- The CLI accepts a single `--mask`.
- Image and mask must be the same size and format and each under 50MB.
- Masks must include an alpha channel.
- If multiple input images are provided, the mask applies to the first image.
- Masking is prompt-guided; do not promise exact pixel-perfect mask boundaries.
- In the edit prompt, repeat invariants (`change only the background; keep the subject
  unchanged`) to reduce drift.

## Batch generation (`generate-batch`)

Reads a JSONL file, one job per line. Each line is either a prompt string or a JSON
object with a required `prompt` and optional `out`, `n`, `size`, `quality`, `model`, and
other per-job overrides.

For the Gemini endpoint a job may also set `aspect_ratio` and `image_size` directly
instead of `size`; the CLI folds them into the `tier@ratio` form.

```bash
bash "$IMAGE_GEN" generate-batch \
  --input tmp/imagegen/jobs.jsonl \
  --out-dir output/imagegen/batch \
  --concurrency 5
```

Jobs are dispatched concurrently, and gemini jobs go over the native endpoint in the same
run as images-endpoint jobs.

- `--out-dir` is required for `generate-batch`.
- `--concurrency` (1-25) controls parallel jobs.
- `--max-attempts` (1-10) retries transient errors.
- `--fail-fast` stops on the first failing job.
- The base model is resolved once from the CLI flags; a job may override it with `model`.
  Jobs whose model emits one image per call are fanned out like `generate`.

## Output handling
- Use `tmp/imagegen/` for temporary JSONL inputs or scratch files.
- Use `output/imagegen/` for final outputs.
- Reruns fail if a target file already exists unless you pass `--force`.
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on.
- Downscaled copies use the default suffix `-web` unless you override it.

## Common recipes

Generate with augmentation fields:

```bash
bash "$IMAGE_GEN" generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case "product-mockup" \
  --style "clean product photography" \
  --composition "wide product shot with usable negative space for page copy" \
  --constraints "no logos, no text" \
  --out output/imagegen/mug-hero.png
```

Generate + also write a downscaled copy for fast web loading:

```bash
bash "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 2048x1152 --downscale-max-dim 1280 \
  --out output/imagegen/alpine-cabin.png
```

Background removal:

```bash
bash "$IMAGE_GEN" generate \
  --task cutout --image product.png --cutout-scene product \
  --out output/imagegen/product-cutout.png
```

Upscale 2x:

```bash
bash "$IMAGE_GEN" generate \
  --task upscale --image small.png --upscale-multiple 2 \
  --out output/imagegen/small-hd.png
```
