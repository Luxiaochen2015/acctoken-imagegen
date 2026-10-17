# acctoken-imagegen

Hermes / agent skill for generating and editing raster images through the **Acctoken**
OpenAI-compatible relay, automatically picking the cheapest model that fits the request.

- Cost-aware resolver: `--task` pins intent (`draft` / `photo` / `text` / `cutout` /
  `upscale` / `4k` / `edit` / …), `--profile cheapest|balanced|best` sets ambition.
- CLI only: everything runs through `scripts/run.sh`.
- Python environment lives **outside** this directory, so the skill stays small.

## Install

```bash
# canonical copy + link into any agent's skills root
git clone <this repo> ~/.agents/skills/acctoken-imagegen
ln -sfn ~/.agents/skills/acctoken-imagegen <agent skills root>/acctoken-imagegen
```

## Credentials (kept outside this repo)

The key is read at runtime, first match wins:

1. `ACCTOKEN_API_KEY`
2. `IMAGE_API_KEY`
3. `OPENAI_API_KEY`
4. `ACCTOKEN_IMAGE_ENV` (points at a dotenv file)
5. `$XDG_CONFIG_HOME/acctoken-imagegen.env` (default `~/.config/acctoken-imagegen.env`)
6. `~/acctoken-imagegen.env`

```bash
printf 'ACCTOKEN_API_KEY=sk-...\n' > ~/.config/acctoken-imagegen.env
chmod 600 ~/.config/acctoken-imagegen.env
```

**No API key is stored in this repository.** `.gitignore` blocks `*.env`, `*.pem`,
`*.key`, `.venv/`, `output/` and logs so credentials and generated assets cannot be
accidentally committed.

## Usage

```bash
SKILL_DIR=~/.agents/skills/acctoken-imagegen
bash "$SKILL_DIR/scripts/run.sh" models
bash "$SKILL_DIR/scripts/run.sh" generate --prompt "A cozy alpine cabin at dawn" --out output/imagegen/cabin.png
bash "$SKILL_DIR/scripts/run.sh" generate --task cutout --image in.png --out output/imagegen/cutout.png
bash "$SKILL_DIR/scripts/run.sh" edit --image in.png --prompt "Replace only the background" --out output/imagegen/edited.png
```

Full documentation: [`SKILL.md`](SKILL.md) and [`references/`](references/).
