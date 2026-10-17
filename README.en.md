# acctoken-imagegen

A **CLI-only skill** for AI agents (Hermes, Claude Code, Codex, pi, …) that generates and
edits raster images through the **Acctoken** OpenAI-compatible relay, automatically picking
the cheapest model that can actually do the job.

Cheapest-first: a plain picture from ¥0.10, a background cutout from ¥0.02, an upscale for
¥0.06, a flagship 4K-capable model for ~¥0.18.

Detailed documentation (Chinese, recommended): **[README.md](README.md)**.

## Why

Most image APIs make you choose the model and worry about the bill. This one inverts that:
you describe the intent (`--task text|cutout|upscale|4k|edit|…`), and a resolver intersects

- the price catalog (`scripts/model_catalog.py`, refreshed from the relay's public
  `/api/pricing`),
- the model ids **your key can actually reach** (`GET /v1/models`, cached 6 h),
- the capability your request needs,

then picks the cheapest match and reports what it chose and what it should cost (stderr).

## Install

```bash
git clone https://github.com/Luxiaochen2015/acctoken-imagegen.git ~/.agents/skills/acctoken-imagegen
ln -sfn ~/.agents/skills/acctoken-imagegen <your agent's skills root>/acctoken-imagegen
```

The Python environment lives outside the skill directory; `scripts/run.sh` locates an
interpreter that can import `openai`, `pillow`, `httpx` and bootstraps one with `uv` if
needed. Overrides: `ACCTOKEN_IMAGEGEN_PYTHON`, `ACCTOKEN_IMAGEGEN_VENV`,
`ACCTOKEN_IMAGEGEN_HOME`.

## Credentials

The key is resolved at runtime and is **never stored in this repo**:

1. `ACCTOKEN_API_KEY` → 2. `IMAGE_API_KEY` → 3. `OPENAI_API_KEY` →
4. file pointed at by `ACCTOKEN_IMAGE_ENV` → 5. `~/.config/acctoken-imagegen.env` →
6. `~/acctoken-imagegen.env` → 7. legacy agent files (`~/.codex/`, `~/.pi/agent/`,
`~/.claude/acctoken-imagegen.env`).

```bash
printf 'ACCTOKEN_API_KEY=sk-...\n' > ~/.config/acctoken-imagegen.env
chmod 600 ~/.config/acctoken-imagegen.env
```

## Usage

```bash
GEN=~/.agents/skills/acctoken-imagegen/scripts/run.sh

bash $GEN models                                   # catalog + what your key reaches
bash $GEN generate --prompt "A cozy alpine cabin at dawn" --out output/imagegen/cabin.png
bash $GEN generate --task cutout --image in.png --out output/imagegen/cutout.png
bash $GEN generate --task upscale --image small.png --upscale-multiple 2 --out out.png
bash $GEN edit --image in.png --prompt "Replace only the background; keep the subject unchanged" --out out.png
bash $GEN generate --prompt "Test" --dry-run        # free: prints resolved model + payload
```

Cost knobs: `--task`, `--profile cheapest|balanced|best`, `--max-cost <CNY>`,
`--thinking minimal|high`, `--n`.

## Notes

- Models that bill a flat CNY amount per image (`seedream`, `qwen-image`, `wan2.7`,
  `z-image`, `image-cutout`, `image-hd`) cost nothing extra for a bigger size or higher
  quality. `gpt-image-2.5` and `gemini-nano-banana-2.1` are token-priced — size and
  quality matter.
- `qwen-image-3.0` / `3.0-pro` are deliberately excluded: 123.3 s per image, over the
  120 s request timeout. The CLI refuses those ids with the reason.
- On `edit`, omitting `--size` does not preserve pixel dimensions; pass `--size` when the
  asset must match exactly.
- `gemini-nano-banana-2.1` always stamps SynthID + a C2PA credential and cannot take
  `--mask`.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Acctoken, OpenAI, Google, ByteDance or
Alibaba Cloud; model names and trademarks belong to their respective owners.
