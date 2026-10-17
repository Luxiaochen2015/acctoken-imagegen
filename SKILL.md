---
name: acctoken-imagegen
description: "Generate or edit raster images via the Acctoken API relay, picking the cheapest suitable model automatically (seedream, qwen-image, wan2.7, z-image, image-cutout, image-hd, gpt-image-2.5, gemini-nano-banana-2.1). Use for photos, illustrations, textures, mockups, UI mockups, logos, infographics, background cutouts, upscaling, and fusing several reference images when the built-in image tool is unavailable."
---

# Acctoken Image Generation Skill

Generates or edits images through the **Acctoken** OpenAI-compatible API relay for the
current project (website assets, game assets, UI mockups, product mockups, wireframes,
logo design, photorealistic images, infographics, cutouts, and so on).

This skill is **CLI-only**: every image operation runs through the bundled
`scripts/run.sh`, which invokes `scripts/image_gen.py` with a Python environment that
lives outside the skill directory (see Runtime and portability below). The built-in image
tool is not used, because the request must go to the Acctoken endpoint with the configured
model and key.

## Cost policy: cheapest that fits, chosen per request

The relay bills most Chinese models a **flat CNY amount per delivered image**
(`seedream-5.0-flash` ¥0.12, `z-image-turbo` ¥0.10, `image-cutout` ¥0.02, `image-hd`
¥0.06, …), and bills `gpt-image-2.5` and `gemini-nano-banana-2.1` **per token**. On the
flat-priced models a bigger size or a higher `quality` is free; on the token-priced models
it is not.

`--model` defaults to **`auto`**: the CLI resolves it to the cheapest model **this API key
can actually reach** that satisfies the request, and prints what it chose and the estimated
cost on stderr. Exclude or include models by intent with `--task`, and raise the ambition
with `--profile` (see below). Run `scripts/run.sh models` to see the catalog, the prices,
and which ids your key can reach.

- The default posture is `--profile cheapest`. Money is the only axis, so a plain picture
  goes to `z-image-turbo` (¥0.10) or `seedream-5.0-flash` (¥0.12), a cutout to
  `image-cutout` (¥0.02), an upscale to `image-hd` (¥0.06).
- `--task` names the intent so the cheapest *capable* model wins: `draft`, `general`,
  `photo`, `illustration`, `text` (dense / CJK in-image text), `edit`, `cutout`,
  `upscale`, `layers`, `4k`. `auto` (default) infers `edit` when an input image is
  involved and `general` otherwise. This is how you turn a ¥0.12 seedream job into a
  ¥0.02 cutout instead of a generic generation.
- `--profile balanced` / `--profile best` trade money for capability (dense text, 4K,
  best instruction following) when the asset is final rather than exploratory. On a key
  that can reach it, `gemini-nano-banana-2.1` is the cheapest flagship (~¥0.18 at 1K),
  so it is what balanced/best resolve to at 1K–2K.
- `--max-cost 0.5` refuses to auto-select a model whose estimate exceeds that many CNY.
- `--thinking minimal|high` sets the thinking budget where a model has one
  (`gemini-nano-banana-2.1`); `minimal` is the default because the relay bills thinking
  tokens as output.
- Pin an id (`--model seedream-5.0-pro`) to override the resolver entirely.

**Latency is part of the fit, and measuring it costs nothing.** The whole `qwen-image-3.x`
line is out of the catalog and the CLI **refuses** both ids (`--model qwen-image-3.0` and
`--model qwen-image-3.0-pro` exit with the reason; `models` lists them under "Deliberately
not offered"). `qwen-image-3.0` was measured at **123.3 s** for one 1024×1536 image —
against 13–19 s for `seedream-5.0-flash` and 15.4 s for `gemini-nano-banana-2.1` — so it
blew the 120 s request timeout. `qwen-image-3.0-pro` was removed at the owner's request on
shared-upstream evidence: same Bailian backend, documented at ~5 requests/minute, but never
timed directly (its exclusion note says so). For in-image text use `gemini-nano-banana-2.1`
(1K ~¥0.18) or `gpt-image-2.5`. If a run is expected to be slow anyway (4K, several
reference images), background it rather than sitting on the timeout.

Every request already prints its wall-clock time to **stderr** — `Generation completed in
12.4s.`, `Edit completed in …`, and `[job i/n] completed in …` for batches — so redirect
stderr to a log and those lines *are* your measurements:

```bash
bash "$IMAGE_GEN" generate --prompt "…" --out out.png 2>&1 | tee run.log
grep -rh "completed in" run.log /path/to/other-logs | sort | uniq -c   # harvest, do not re-run
```

**Never re-run a model just to time it.** The user pays per call; repeating an
already-measured run buys no new information. Harvest timings from existing logs first,
and only when a model has no recorded timing at all is one probe justifiable — and then
ask before spending. The free surfaces cover almost everything: `--dry-run` (resolver and
payload, no request), `scripts/run.sh models` (catalog, prices, reachable ids, speeds), and
`bash scripts/update_pricing.sh` (public `/api/pricing`, no key). Record findings in
`latency_s` on the catalog entry; anything unmeasured stays `unmeasured`, never assumed
fast.

**A deliberate removal is not an unknown model.** The resolver has two "not in the
catalog" paths and conflating them lets a removed model quietly come back: an id the
catalog has never priced is passed through to the relay with a warning, so a model dropped
for a real reason must *also* be listed in `EXCLUDED_MODELS` with its reason — only then
does `--model <id>` fail loudly instead of routing work to it. `update_pricing.py` reads
the same dict and writes a "Deliberately not offered" section rather than re-adding the id
to `references/models.md`; skip that and the next price refresh silently restores what you
just removed. Pruning is a judgement call: measured latency is the strongest evidence, a
sibling on the same upstream and rate limit is weaker — when you act on the weaker kind
(or the owner tells you to), put that basis in the exclusion note instead of presenting it
as a measurement.

Cost is resolved **per request and per size**, not per model. `--task text --size 1024x1024`
and `--task text --size 2048x2048` can resolve to different models, because the token-priced
candidates get more expensive with size while the flat-priced ones do not.

Never guess a model your key cannot use: the resolver probes `GET /v1/models` (cached under
the runtime home for 6 hours; `scripts/run.sh models --refresh` re-probes immediately) and
only picks from the reachable set. To refresh the catalog prices, run
`bash scripts/update_pricing.sh` (public endpoint, no key needed).

## Configuration

- Base URL: `https://www.acctoken.com/v1`
- Model: `auto` (cost-aware resolver). Pin a catalog id such as `seedream-5.0-flash`,
  `seedream-5.0-pro`, `gpt-image-2.5`, `qwen-image-2.0`, or `gemini-nano-banana-2.1`
  to bypass it. Model ids the catalog does not know are passed through with a warning;
  ids that were removed on purpose (the `qwen-image-3.x` line, too slow) are rejected
  with the reason instead.
- Endpoints: `/v1/images/generations` + `/v1/images/edits` for every model except
  `gemini-nano-banana-2.1`, which the relay serves only over the Gemini native
  `/v1beta/models/gemini-nano-banana-2.1:generateContent` (the CLI handles this for you).
- Dependencies: `openai`, `pillow`, `httpx`. `scripts/run.sh` resolves a Python
  interpreter that can import them (see Runtime and portability below). No
  per-project install is normally needed.

API key resolution (first match wins):

1. Environment variable `ACCTOKEN_API_KEY`
2. Environment variable `IMAGE_API_KEY`
3. Environment variable `OPENAI_API_KEY`
4. File `ACCTOKEN_IMAGE_ENV`, if set (search stops here: nothing else is consulted)
5. `$XDG_CONFIG_HOME/acctoken-imagegen.env`, defaulting to `~/.config/acctoken-imagegen.env`,
   or the XDG-style `$XDG_CONFIG_HOME/acctoken-imagegen/env`
6. `~/acctoken-imagegen.env`, or `~/acctoken-imagegen/env`
7. Legacy per-agent files, kept only so old installs keep working: `~/.codex/`,
   `~/.pi/agent/`, `~/.claude/acctoken-imagegen.env`

Only `ACCTOKEN_API_KEY`, `IMAGE_API_KEY` and `OPENAI_API_KEY` assignments inside those
files are read.

The config file is a simple `KEY=VALUE` dotenv file and should stay `chmod 600`. The
key is **not** stored in the skill scripts, and lives outside the skill directory so
the directory can be copied, synced, or published without leaking it. Never paste it
into prompts, logs, commits, or shared output.

## Runtime and portability

The skill directory holds only Markdown and scripts. The Python environment lives
**outside** it, so copying or moving the skill between agents never breaks an
interpreter, and the skill stays small enough to sync.

`scripts/run.sh` picks the first interpreter that can actually import the required
modules (an executable-but-dead copied venv is rejected), in this order:

1. `$ACCTOKEN_IMAGEGEN_PYTHON` — an explicit interpreter path
2. `$ACCTOKEN_IMAGEGEN_VENV/bin/python` — an explicit virtualenv
3. `<skill dir>/.venv/bin/python` — a venv bundled with the skill, if you prefer
   self-containment over a small skill directory
4. `${ACCTOKEN_IMAGEGEN_HOME:-$HOME/.agents}/venvs/acctoken-imagegen/bin/python` —
   the shared host-local environment
5. `python3` / `python` on `PATH`, if the dependencies are importable there
6. Self-heal: if `uv` is available, create/top up (4) and continue
   (`ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP=1` disables this for CI that must fail fast)

Model-accessibility caching and other runtime state live under
`${ACCTOKEN_IMAGEGEN_HOME:-$HOME/.agents}/cache/acctoken-imagegen/`, never inside the
skill. Set `ACCTOKEN_IMAGEGEN_OFFLINE=1` to skip the `/v1/models` probe (the resolver then
assumes the whole catalog is available).

Nothing in the skill is bound to a specific agent. Install it by putting the directory
wherever your agent scans for skills, and point the CLI at the real path:

```bash
# Canonical copy, one source of truth. Agents that want a private skills root just link it.
mkdir -p ~/.agents/skills && cp -a <this skill> ~/.agents/skills/acctoken-imagegen
ln -sfn ~/.agents/skills/acctoken-imagegen <agent skills root>/acctoken-imagegen
```

## Modes

The CLI exposes four subcommands:

- `generate` — create a new image from a prompt (optionally with reference / input images)
- `edit` — modify one or more existing images (optional mask)
- `generate-batch` — run many generation jobs concurrently from a JSONL file
- `models` — list the model catalog, prices, and which ids this key can reach

## Workflow

1. Decide the intent: `generate` (new image) or `edit` (modify an existing image).
   - If the user provides images only as style/composition/mood references, treat it as
     `generate --image ...`. If they ask to change parts of an existing image, treat it as
     `edit`.
   - Background removal → `--task cutout`; upscaling → `--task upscale`; layer
     separation → `--task layers`; dense/CJK in-image text → `--task text`.
   - Fusing **several** reference images into one picture, or a conversational "change this
     part" edit, is what `gemini-nano-banana-2.1` is best at: `--profile balanced` (or pin
     `--model gemini-nano-banana-2.1`) reaches it. It is also the cheapest flagship and much
     faster than the qwen-image models. Remember it cannot take a `--mask`, always stamps
     SynthID/C2PA, and returns a 1K/2K/4K tier rather than exact pixel dimensions.
2. Collect inputs up front: prompt, any exact text (verbatim), constraints/avoid list,
   and input images. Label each input image by index and role.
3. If the prompt is generic, add only the detail that materially improves it (composition,
   polish level, practical layout). If it is already detailed, normalize it instead of
   expanding it.
4. Pick the posture: leave `--profile` at `cheapest` (default) for drafts and bulk assets;
   add `--task` so the cheapest *capable* model is chosen; use `--profile balanced|best`
   only when the asset is final. Do not change the model or base URL by hand unless the
   user asks — pin `--model` when they name one.
5. For transparent outputs, prefer `--task cutout` (if the key reaches `image-cutout`) or
   `--background transparent`; verify the alpha channel. If the endpoint rejects
   transparency, generate on a flat chroma-key background plus `scripts/run_chroma.sh`.
6. Inspect the result (subject, style, composition, text accuracy, invariants).
7. Iterate with a single targeted change, then re-check.
8. Save final artifacts under `output/imagegen/` in the current project (or the path the
   user named). Do not overwrite existing files unless the user asked for replacement —
   otherwise use a versioned sibling name such as `hero-v2.png`.
9. Report the final saved path(s), the resolved model, and the estimated cost — the user
   tracks spend per deliverable, so state it without being asked.

## Commands

```bash
# Resolve the CLI from wherever this SKILL.md lives, not from a fixed agent home.
SKILL_DIR="$(cd "$(dirname "<path to this SKILL.md>")" && pwd)"
export IMAGE_GEN="$SKILL_DIR/scripts/run.sh"

# See the catalog, prices, and what this key can reach
bash "$IMAGE_GEN" models

# Dry-run (no network request, no cost): prints the resolved model and the payload
bash "$IMAGE_GEN" generate --prompt "Test" --out output/imagegen/test.png --dry-run

# Generate (auto model: cheapest fit, here seedream-5.0-flash at ~¥0.12)
bash "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --out output/imagegen/alpine-cabin.png

# Generate with an explicit size and a final-asset posture
bash "$IMAGE_GEN" generate \
  --prompt "A polished landing-page hero of a matte ceramic mug" \
  --task photo --profile balanced --size 2048x1152 \
  --out output/imagegen/mug-hero.png

# Transparent cutout (¥0.02 if image-cutout is reachable)
bash "$IMAGE_GEN" generate --task cutout --image input.png \
  --out output/imagegen/cutout.png

# Edit (size omitted: the relay picks the output resolution)
bash "$IMAGE_GEN" edit \
  --image input.png \
  --prompt "Replace only the background with a warm sunset; keep the subject unchanged" \
  --out output/imagegen/sunset-edit.png

# Edit with exact output dimensions pinned
bash "$IMAGE_GEN" edit \
  --image input.png \
  --size 1024x1536 \
  --prompt "Replace only the background with a warm sunset; keep the subject unchanged" \
  --out output/imagegen/sunset-edit.png
```

### Model selection flags

- `--model auto` (default) or an explicit id.
- `--task auto|draft|general|photo|illustration|text|edit|cutout|upscale|layers|4k`.
- `--profile cheapest|balanced|best` (default `cheapest`).
- `--max-cost <CNY>` to cap the auto-selected model's estimated cost.
- `--thinking minimal|high` for models with a thinking knob (default `minimal`).

### Size and `--n`

- On `generate`, an unset `--size` becomes the selected model's own default (usually
  `1024x1024`); some models also accept keywords such as `2K`/`4K`.
- **`--size auto` is not accepted by the seedream family, the edits endpoint, or the
  Gemini endpoint** (the relay only allows it for layer decomposition). Passing it where it
  is not allowed fails locally with a usable hint instead of a relay 400.
- seedream sizes need **≥ 921,600 total pixels** (for example `1024x1024` works, `512x512`
  does not); `gpt-image-2.5` needs multiples of 16 and ~832×832 minimum.
- `gemini-nano-banana-2.1` sizes by **resolution tier + aspect ratio** (`1K`/`2K`/`4K`), not
  by pixel dimensions: pass `--size 16:9` for the ratio at the default tier, `--size 2K`
  for a square 2K, `--size 2K@16:9` for both, or a `WIDTHxHEIGHT` that is snapped to the
  nearest supported ratio and the tier covering its long edge. Ask for
  `--size 2048x1152` and you get a 2K 16:9 picture.
- `--n` works everywhere: models that only emit one image per call (seedream,
  gpt-image, gemini) are fanned out into `n` separate calls automatically, and the cost is
  `n ×` the per-image price. batch jobs fan out the same way.

Size notes for `edit`: omitting `--size` does **not** keep the input's pixel dimensions —
the parameter is dropped and the relay chooses the resolution, preserving the aspect ratio
but possibly rescaling (a `1024x1536` input returned `1664x2496`). If the asset must match
an exact size, pass `--size` and verify the saved file's dimensions.

`scripts/run.sh` resolves its own interpreter, so no per-project install is needed.
If nothing usable is found it prints the three ways to fix it (bootstrap with `uv`,
`ACCTOKEN_IMAGEGEN_VENV`, or `ACCTOKEN_IMAGEGEN_PYTHON`) and exits non-zero; it never
silently runs against a half-installed environment.

## Prompt shaping

Reformat the user's goal into a short, structured spec. Use only the lines that help:

```text
Use case: <taxonomy slug>
Asset type: <where the asset will be used>
Primary request: <user's main prompt>
Input images: <Image 1: role; Image 2: role>   (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/etc>
Composition/framing: <wide/close/top-down; placement>
Lighting/mood: <lighting + mood>
Color palette: <palette notes>
Materials/textures: <surface details>
Text (verbatim): "<exact text>"
Constraints: <must keep/must avoid>
Avoid: <negative constraints>
```

Rules:
- Keep it short; add only what materially improves the result.
- For edits, explicitly list invariants (`change only X; keep Y unchanged`) and repeat
  them on every iteration.
- Match the user's specificity: normalize detailed prompts, lightly augment generic ones.
- Never invent extra characters, props, brands, or narrative beats.

## When not to use
- Extending or matching an existing SVG/vector icon set, logo system, or illustration
  library inside the repo.
- Creating simple shapes, diagrams, wireframes, or icons that are better produced
  directly in SVG, HTML/CSS, or canvas.
- Editing an existing asset that is already in an editable native format.
- Any task where the user clearly wants deterministic code-native output.

## References
- `references/models.md` — generated catalog: every image model, price, and capability.
- `references/prompting.md` — prompt structure, specificity, invariants, iteration.
- `references/sample-prompts.md` — copy/paste prompt recipes by use case.
- `references/cli.md` — full CLI surface, defaults, flags, batch usage, key setup.
- `references/image-api.md` — API parameters, sizes, and per-model limits.
- `references/network-environments.md` — network/sandbox approval notes for live calls.
