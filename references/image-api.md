# API parameter reference

This skill calls an OpenAI-compatible Images API at `https://www.acctoken.com/v1`
through `scripts/run.sh` (which calls `scripts/image_gen.py`). These parameters describe
that surface. Prices and per-model limits are generated into `references/models.md`.

`gemini-nano-banana-2.1` is the one exception: the relay serves it only over the
**Gemini native** endpoint, `POST /v1beta/models/gemini-nano-banana-2.1:generateContent`.
See "The Gemini endpoint" below.

## Endpoints
- `POST /v1/images/generations` — text-to-image, reference-image generation, cutout, upscale
- `POST /v1/images/edits` — image editing (one or more input images, optional mask)
- `POST /v1beta/models/<id>:generateContent` — the Gemini-native endpoint (gemini-nano-banana-2.1)
- `GET /v1/models` — the model ids this API key may use (the resolver's source of truth)
- `GET /api/pricing` — public price catalog (what `scripts/update_pricing.sh` reads)

## Models

`--model` defaults to `auto`: the cheapest model the key can reach that fits the request.
The full catalog with prices, capabilities, and per-model limits is in
`references/models.md`; the machine-readable version is `scripts/model_catalog.py`.

| Model | Kind | Capabilities | `n` | Size notes |
| --- | --- | --- | --- | --- |
| `seedream-5.0-flash` | ¥0.12/image | t2i, edit, ref | 1 | `2K` or WxH, ≥ 921,600 px |
| `seedream-5.0-pro` | ¥0.54/image | t2i, edit, ref, 4K | 1 | `2K` or WxH, ≥ 921,600 px |
| `seedream-5.0-pro-layers` | ¥0.15/layer | edit, layers, transparent | 1 | `1K`/`1.5K`/`2K`/`auto` |
| `qwen-image-2.0` | ¥0.15/image | t2i, edit, ref, text | 6 | 512–2048 |
| `qwen-image-2.0-pro` | ¥0.38/image | t2i, edit, ref, text | 6 | 512–2048 |
| `wan2.7-image` | ¥0.15/image | t2i, edit, ref | 4 | `1K`/`2K` |
| `wan2.7-image-pro` | ¥0.38/image | t2i, edit, ref, 4K | 4 | `1K`/`2K`/`4K` |
| `z-image-turbo` | ¥0.10/image | t2i, text | — | 512–2048 |
| `image-cutout` | ¥0.02/image | cutout, transparent | 1 | input image required |
| `image-hd` | ¥0.06/image | upscale | 1 | input long edge ≤ 2048 |
| `gpt-image-2.5` | token | t2i, edit, ref, text, 4K, transparent | 1 | multiples of 16, ≤ 3840, ratio ≤ 3:1 |
| `gemini-nano-banana-2.1` | token | t2i, edit, ref, text, 4K | 1 | `1K`/`2K`/`4K` tier + aspect ratio |

Notes:
- Flat-priced (`¥…/image`) models do not price on `size` or `quality`.
- `gpt-image-2.5` and `gemini-nano-banana-2.1` are token-priced. `gpt-image-2.5` is served
  over `/v1/images/generations`; `gemini-…` is served over `/v1beta/…:generateContent`.
- `qwen-image-*` does not support `input_fidelity`.
- Models capped at `n=1` are fanned out by the CLI into `n` calls.

## Shared parameters
- `model`: `auto` (default) or a model id
- `prompt`: text prompt (the CLI can also augment this with structured fields)
- `n`: number of images, 1-10 (fanned out per model caps)
- `size`: `WIDTHxHEIGHT` / `WIDTH*HEIGHT`, a model keyword (`2K`, `4K`), a bare aspect
  ratio (`16:9`, only on the Gemini endpoint), or `auto` where allowed. **`auto` is only
  valid for layer decomposition**; the seedream family, the edits endpoint, and the Gemini
  endpoint reject it, so it fails locally instead of at the relay. On the Gemini endpoint a
  `WIDTHxHEIGHT` value is mapped onto the closest aspect ratio plus a resolution tier.
- `quality`: `low`, `medium`, `high`, or `auto` (prices only `gpt-image-2.5`)
- `background`: output transparency behavior (`transparent`, `opaque`, or `auto`);
  this is not the same thing as the prompt's visual scene/backdrop
- `output_format`: `png` (default), `jpeg`, `webp`
- `output_compression`: 0-100 (jpeg/webp only)
- `thinking`: `minimal` (default) or `high`. Only models with a thinking knob accept it
  (currently `gemini-nano-banana-2.1`); the relay bills thinking tokens as output, so
  `high` adds roughly 820 tokens, about +¥0.13 at 1K.

## Generate-only parameters
- `moderation`: `auto` (default) or `low`. The edits endpoint does not accept it, so the
  CLI rejects `--moderation` combined with `edit` instead of letting the SDK raise.
- `watermark`: `false` on models that add one (`seedream-*`). The CLI sends
  `watermark=false` by default so assets are not stamped; pass `--watermark` to keep the
  relay default.
- `image`: one or more reference / utility input images, as data URLs. The CLI exposes this
  as `--image` on `generate`. It is sent in the request body via the SDK `extra_body`
  channel, alongside `scene` and `multiple` (which the SDK's `images.generate()` signature
  does not accept as keywords).
- `scene`: `general` | `human` | `product` for `image-cutout` (`--cutout-scene`).
- `multiple`: `2` | `3` | `4` for `image-hd` (`--upscale-multiple`).

## Edit-specific parameters
- `image`: one or more input images (repeated `--image`). Order is meaningful.
- `mask`: optional mask image
- `input_fidelity`: `low` or `high` only for models that support it; the CLI rejects it
  locally for the `gpt-image-2.x` and `qwen-image-` families rather than letting the
  relay error or silently ignore it. For other models it is passed through to the relay.

## Transparent backgrounds
`--background transparent` is forwarded to the endpoint. It may or may not be
supported by the relay for a given model. The cheapest reliable route to a transparent
asset is `--task cutout` (the `image-cutout` model, ¥0.02) when the key can reach it. If
neither works, generate on a flat chroma-key background and extract alpha locally:

```bash
bash "$SKILL_DIR/scripts/run_chroma.sh" \
  --input chroma.png --out cutout.png
```

## The Gemini endpoint (`gemini-nano-banana-2.1`)

This model is not on `/v1/images/generations`. The relay exposes it at
`POST /v1beta/models/gemini-nano-banana-2.1:generateContent`, and that is the only path
that works properly — see "Why not the chat endpoint" below.

Request shape (what the CLI builds):

```json
{
  "contents": [
    { "parts": [
        { "text": "<prompt>" },
        { "inlineData": { "mimeType": "image/png", "data": "<base64>" } }
    ] }
  ],
  "generationConfig": {
    "imageConfig":       { "aspectRatio": "16:9", "imageSize": "2K" },
    "thinkingConfig":    { "thinkingLevel": "minimal" }
  }
}
```

- **Reference / input images** are extra `parts` with `inlineData` (base64, no data-URL
  prefix). Several images can be fused in one request — the model's strength.
- **`aspectRatio`** ∈ `1:1 | 16:9 | 9:16 | 4:3 | 3:4 | 4:5 | 5:4 | 3:2 | 2:3 | 21:9`.
  On the CLI, `--size` accepts a bare ratio (`16:9`), a `tier@ratio` pair (`2K@16:9`), a
  lone tier (`2K` → 1:1), or a `WIDTHxHEIGHT` that is snapped to the nearest ratio and tier.
- **`imageSize`** ∈ `1K | 2K | 4K`; measured output is 1K ≈ 1120 image tokens,
  2K ≈ 1680, 4K ≈ 3780.
- **`thinkingLevel`** — `minimal` emits no thinking tokens; `high` added ~820 tokens at
  1K. The CLI defaults to `minimal` and exposes `--thinking high`.
- **Response**: `candidates[0].content.parts[]` holds one or more images, as
  `inlineData.data` (base64 PNG). **The model returns a low-resolution draft first and
  the finished image last — the CLI keeps only the last image per call.**
- **No mask, no `quality`, no `background`, no `output_format`** (always PNG), no
  `moderation`. The CLI warns when you set a flag it cannot honour.
- **Every output carries a Google C2PA credential and a SynthID watermark** that cannot
  be switched off. They are not a visible stamp, but the file is provably marked.

### Why not the chat endpoint
The relay's pricing blurb points at `POST /v1/chat/completions` with
`extra_body.google.image_config`. Measured on 2026-10-17, that path **silently ignores
`image_config`**: a `1:1` request and a `21:9` request both came back `1376x768`, and the
chat path also billed ~700 thinking tokens that `thinkingLevel: minimal` removes on the
native path. The native endpoint honoured `21:9` (→ `1584x672`) and `3:4` (→ `896x1200`)
exactly. Use the native endpoint.

## Cost estimation
`scripts/model_catalog.py` estimates each request. Flat-priced models are exact
(`price × n`).

Token-priced models are derived from the relay's own ratios. `GET /api/pricing` gives each
model a `model_ratio`, a `completion_ratio` and (sometimes) an `image_ratio`, and the
relay bills

```
cost_CNY = (prompt_tokens + completion_tokens × completion_ratio) × model_ratio / 500000
```

which is the same as `ratio × 2 CNY per million tokens` for text input, output, and image
input respectively. This reproduces `gpt-image-2.5`'s published rates (¥56 / ¥89.6 / ¥336
per million) and its documented per-image prices exactly, so the same rule is applied to
`gemini-nano-banana-2.1`: **text in ¥7.96/M, reference image ¥7.96/M, image out
¥159.23/M**, giving about **¥0.18 at 1K, ¥0.27 at 2K, ¥0.60 at 4K**, plus ¥0.009 per
reference image.

`gpt-image-2.5` is area-priced within that model: a 1024×1024 output-token baseline scaled
by pixel count gives ~¥0.15 at 1024×1024 medium, ~¥0.59 high, ~¥1.2 at 2048×2048 high.
`gemini-nano-banana-2.1` is tier-priced (1K/2K/4K), so the pixel count is snapped to a tier
rather than scaled.

The `generate --dry-run` output and the `models` subcommand print these estimates.
`scripts/update_pricing.sh` also checks the catalog's token rates against the live ratios
and reports drift.

## Output
- The images endpoint returns one `b64_json` (or `url`) per image in `data[]`.
- The Gemini endpoint returns `candidates[0].content.parts[].inlineData.data` (base64 PNG);
  the last image part is the finished one.
- The CLI decodes / downloads the image and writes the output files for you.

## Limits and notes
- Input images and masks must be under 50MB (utility models have tighter limits:
  `image-hd` 10MB, `image-cutout` 30MB).
- Use the edits endpoint when the user requests changes to an existing image.
- Masking is prompt-guided; exact shapes are not guaranteed. The Gemini endpoint has no
  mask parameter at all (`--mask` fails locally).
- Larger sizes and higher quality increase latency, and increase cost only on token-priced
  models.
- The model-accessibility list from `GET /v1/models` is cached for 6 hours. If a key gains
  access to a new model, the resolver keeps ignoring it until the cache expires — run
  `scripts/run.sh models --refresh` to pick it up immediately.
- If a request fails because a specific option is unsupported, retry without that option
  only when the option is not required by the user.
