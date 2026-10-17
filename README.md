# acctoken-imagegen

> 一个给 AI Agent（Hermes / Claude Code / Codex / pi 等）用的**图像生成与编辑技能包**。
> 通过 Acctoken 的 OpenAI 兼容中转接口出图，并**自动挑选「最便宜又够用」的模型**——
> 一张普通图 ¥0.10 起，抠图 ¥0.02，4K 旗舰约 ¥0.18。

English: a CLI-only skill for image generation/editing over an OpenAI-compatible relay,
with a cost-aware model resolver. See [README.en.md](README.en.md).

```bash
# 30 秒上手（假设已配置好 API Key）
git clone https://github.com/Luxiaochen2015/acctoken-imagegen.git ~/.agents/skills/acctoken-imagegen
printf 'ACCTOKEN_API_KEY=sk-你的key\n' > ~/.config/acctoken-imagegen.env && chmod 600 ~/.config/acctoken-imagegen.env
bash ~/.agents/skills/acctoken-imagegen/scripts/run.sh generate \
  --prompt "A cozy alpine cabin at dawn" --out output/imagegen/cabin.png
```

---

## 目录

- [核心特性](#核心特性)
- [模型与价格](#模型与价格)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置 API Key](#配置-api-key)
- [快速开始](#快速开始)
- [命令参考](#命令参考)
  - [`generate` 生成](#generate-生成)
  - [`edit` 编辑](#edit-编辑)
  - [`generate-batch` 批量](#generate-batch-批量)
  - [`models` 模型目录](#models-模型目录)
- [任务类型 `--task` 怎么选](#任务类型---task-怎么选)
- [尺寸规则 `--size`](#尺寸规则---size)
- [省钱的正确姿势](#省钱的正确姿势)
- [常用配方](#常用配方)
- [退出码与错误处理](#退出码与错误处理)
- [常见问题排查](#常见问题排查)
- [安全说明](#安全说明)
- [仓库结构](#仓库结构)
- [更新价格表](#更新价格表)
- [License](#license)

---

## 核心特性

| 特性 | 说明 |
| --- | --- |
| **成本感知选模** | `--model` 默认 `auto`：在「你的 Key 真的能访问」∩「能力满足需求」的模型里选最便宜的，并把选了什么、预估花多少钱打到 stderr |
| **意图驱动** | `--task text/cutout/upscale/4k/...` 决定能力下限。同一句 prompt，加 `--task cutout` 就能从 ¥0.12 的通用模型转成 ¥0.02 的抠图模型 |
| **纯 CLI** | 全部操作走 `scripts/run.sh`，任何 Agent、任何项目目录都能调，不依赖某个 Agent 的私有接口 |
| **自带环境探测** | `scripts/run.sh` 会自己找一个能 `import openai, pillow, httpx` 的 Python；找不到且装了 `uv` 就自动建一个 |
| **密钥零入库** | 脚本不存 Key，Key 只从环境变量或 `~/.config/acctoken-imagegen.env` 读；仓库自带 `.gitignore` + pre-commit 钩子防误提交 |
| **可移植** | 技能目录只放 Markdown 和脚本，虚拟环境放在目录外，随便复制/同步都不会坏 |

## 模型与价格

运行 `scripts/run.sh models` 会打印**你这把 Key 实际能访问**的模型及价格。当前目录快照：

| 模型 | 计价 | 层级 | 能力 | 说明 |
| --- | --- | --- | --- | --- |
| `image-cutout` | ¥0.02/图 | 2 | 抠图 / 透明底 / 编辑 | 去背景神器，输出透明 PNG/WebP |
| `image-hd` | ¥0.06/图 | 2 | 放大 2–4× | 输入长边 ≤ 2048 |
| `z-image-turbo` | ¥0.10/图 | 2 | 文生图 / 文字 | 最便宜的纯文生图 |
| `seedream-5.0-flash` | ¥0.12/图 | 2 | 文生图 / 编辑 / 参考图 | **默认首选**，约 16 s，参考图免费 |
| `seedream-5.0-pro-layers` | ¥0.15/层 | 3 | 图层拆分 | 1 底图 + 最多 16 个透明图层，按层计费 |
| `wan2.7-image` | ¥0.15/图 | 3 | 文生图 / 编辑 / 参考图 | 单次最多 4 张 |
| `qwen-image-2.0` | ¥0.15/图 | 2 | 文生图 / 编辑 / 参考图 / 文字 | 512–2048 px，单次最多 6 张 |
| `wan2.7-image-pro` | ¥0.38/图 | 4 | 文生图 / 编辑 / 4K | 指令遵循更好 |
| `qwen-image-2.0-pro` | ¥0.38/图 | 3 | 文生图 / 编辑 / 文字 | 海报、排版；约 2 请求/分钟 |
| `seedream-5.0-pro` | ¥0.54/图 | 4 | 文生图 / 编辑 / 4K | 2K 旗舰，按 2K 档一口价 |
| `gpt-image-2.5` | 按 token（1024² medium ≈ ¥0.15） | 4 | 全能力 + 中英文字 | 英文+中文字形最稳，越大越贵 |
| `gemini-nano-banana-2.1` | 按 token（1K ≈ ¥0.18 / 2K ≈ ¥0.27 / 4K ≈ ¥0.60） | 4 | 全能力 + 文字 | **最便宜旗舰**，约 15 s，多参考图融合最强 |

> 具体能用哪些、单价多少以 `scripts/run.sh models` 的实测输出为准——不同 Key 的可达范围不同。
> `qwen-image-3.x` 两个模型已被**主动移除**（实测 123 s/图，命中 120 s 请求超时），CLI 会直接拒绝并给出原因。

## 环境要求

- macOS / Linux，`bash`
- Python 3.9+，需要 `openai`、`pillow`、`httpx`
- **不用手动装依赖**：`scripts/run.sh` 会依次尝试
  1. `$ACCTOKEN_IMAGEGEN_PYTHON`
  2. `$ACCTOKEN_IMAGEGEN_VENV/bin/python`
  3. `<技能目录>/.venv/bin/python`
  4. `$HOME/.agents/venvs/acctoken-imagegen/bin/python`
  5. `PATH` 上的 `python3` / `python`
  6. 以上都没有且系统有 `uv` → 自动在 (4) 建环境并装依赖

  显式建环境（可选）：

  ```bash
  uv venv --relocatable "$HOME/.agents/venvs/acctoken-imagegen"
  uv pip install --python "$HOME/.agents/venvs/acctoken-imagegen/bin/python" openai pillow httpx
  ```

- 需要一个 **Acctoken API Key**（`https://www.acctoken.com`）。真实出图需要联网；`--dry-run` 不需要。

## 安装

### 方式 A：装成 Agent 技能（推荐）

```bash
git clone https://github.com/Luxiaochen2015/acctoken-imagegen.git ~/.agents/skills/acctoken-imagegen
# 再软链到你所用 Agent 的技能目录
ln -sfn ~/.agents/skills/acctoken-imagegen ~/.hermes/skills/acctoken-imagegen
# 其它 Agent 同理：
#   Claude Code: ~/.claude/skills/acctoken-imagegen
#   Codex:       ~/.codex/skills/acctoken-imagegen
#   pi:          ~/.pi/agent/skills/acctoken-imagegen
```

Agent 读到 `SKILL.md` 就知道何时该用它、怎么调、怎么控成本。

### 方式 B：当普通命令行工具用

```bash
git clone https://github.com/Luxiaochen2015/acctoken-imagegen.git ~/tools/acctoken-imagegen
alias imagegen='bash $HOME/tools/acctoken-imagegen/scripts/run.sh'
imagegen generate --prompt "a red fox in snow" --out fox.png
```

### 方式 C：不克隆，直接用当前目录

把 `scripts/` 拷进任意项目，调用时改成对应路径即可。

## 配置 API Key

密钥按以下顺序查找，**第一个命中的生效**：

1. `ACCTOKEN_API_KEY` 环境变量
2. `IMAGE_API_KEY` 环境变量
3. `OPENAI_API_KEY` 环境变量
4. `ACCTOKEN_IMAGE_ENV` 指向的 dotenv 文件（一旦设置就**只**读它，别的都不看）
5. `$XDG_CONFIG_HOME/acctoken-imagegen.env`（默认 `~/.config/acctoken-imagegen.env`），或 XDG 风格 `$XDG_CONFIG_HOME/acctoken-imagegen/env`
6. `~/acctoken-imagegen.env`，或 `~/acctoken-imagegen/env`
7. 兼容旧安装：`~/.codex/`、`~/.pi/agent/`、`~/.claude/acctoken-imagegen.env`

dotenv 文件里只会读取 `ACCTOKEN_API_KEY`、`IMAGE_API_KEY`、`OPENAI_API_KEY` 这三个赋值。

```bash
# 推荐：写在用户配置目录，且只给自己可读
mkdir -p ~/.config
printf 'ACCTOKEN_API_KEY=sk-xxxx\n' > ~/.config/acctoken-imagegen.env
chmod 600 ~/.config/acctoken-imagegen.env

# 或者临时用环境变量
export ACCTOKEN_API_KEY="sk-xxxx"

# 或者把 Key 文件放在任意位置再指过去
export ACCTOKEN_IMAGE_ENV=/secure/path/acctoken-imagegen.env
```

验证配置是否可用：

```bash
bash scripts/run.sh models | tail -5
# 末尾会打印 "N model ids reachable by this key" 和 cheapest 解析结果
```

## 快速开始

```bash
SKILL_DIR="$HOME/.agents/skills/acctoken-imagegen"
GEN="$SKILL_DIR/scripts/run.sh"

# 0) 看目录和价格
bash "$GEN" models

# 1) 空跑验证参数（不发请求、不花钱）
bash "$GEN" generate --prompt "Test" --out output/imagegen/test.png --dry-run

# 2) 正式出图
bash "$GEN" generate --prompt "A cozy alpine cabin at dawn" --out output/imagegen/cabin.png

# stdout 打印保存路径、实际使用的模型、预估花费；stderr 打印耗时
```

成功时你会看到类似：

```
Saved: /path/output/imagegen/cabin.png (1024x1024, 1.2 MB)
Model: seedream-5.0-flash (est. CNY 0.12)
Generation completed in 14.8s.      # stderr
```

## 命令参考

统一入口：

```bash
bash scripts/run.sh <generate|edit|generate-batch|models> [flags]
```

### `generate` 生成

| 参数 | 说明 |
| --- | --- |
| `--prompt` / `--prompt-file` | 提示词，或从文件读 |
| `--model` | 默认 `auto`；也可钉死某个 id（如 `seedream-5.0-flash`） |
| `--task` | `auto\|draft\|general\|photo\|illustration\|text\|edit\|cutout\|upscale\|layers\|4k` |
| `--profile` | `cheapest`（默认）/ `balanced` / `best` |
| `--max-cost` | 估价超过该 CNY 值就拒绝自动选模，例如 `--max-cost 0.2` |
| `--thinking` | `minimal`（默认）/ `high`，仅 `gemini-nano-banana-2.1` 有效 |
| `--size` | `1024x1024` / `2K` / 见[尺寸规则](#尺寸规则---size) |
| `--quality` | `low\|medium\|high\|auto`（只有 `gpt-image-2.5` 按此计价） |
| `--n` | 张数；单图模型会自动扇出成 n 次调用，成本 n× |
| `--image` | 参考图，可重复传多个 |
| `--background` | 如 `transparent`、`opaque` |
| `--output-format` / `--output-compression` | 输出格式与压缩率 |
| `--moderation` | 内容审核级别（generate 专用） |
| `--watermark` / `--no-watermark` | 水印开关（seedream 系列默认关） |
| `--cutout-scene` | `general\|human\|product`，配合 `--task cutout` |
| `--upscale-multiple` | `2\|3\|4`，配合 `--task upscale` |
| `--downscale-max-dim` | 额外生成一份缩小副本（网页加载用） |
| `--out` / `--out-dir` | 输出文件 / 目录（目录模式下命名 `image_1.png`…） |
| `--force` | 允许覆盖已存在文件 |
| `--dry-run` | 只解析并打印请求体，不发网络请求 |
| `--augment` / `--no-augment` | 是否把结构化字段拼进 prompt（默认开） |
| `--use-case --scene --subject --style --composition --lighting --palette --materials --text --constraints --negative` | 结构化补全字段，比堆在 `--prompt` 里更稳定 |

### `edit` 编辑

与 `generate` 相同的通用参数，另有：

| 参数 | 说明 |
| --- | --- |
| `--image` | **必填**，可重复传多张；顺序有意义，prompt 里要按序号说明角色 |
| `--mask` | 可选蒙版（必须带 alpha 通道，尺寸/格式与首图一致，< 50 MB）；仅作用于第一张图 |
| `--input-fidelity` | `low\|high`；`gpt-image-2.x` / `qwen-image-` 系列会拒绝 |
| `--size` | 可省略；**省略不等于保持原图尺寸**，详见下方说明 |
| `--moderation` | 编辑接口不支持，传了会本地报错 |

### `generate-batch` 批量

```bash
# jobs.jsonl：一行一个任务
cat > tmp/jobs.jsonl <<'EOF'
"A cozy alpine cabin at dawn"
{"prompt":"A matte ceramic mug on stone","size":"2048x1152","out":"mug.png"}
{"prompt":"A cyberpunk street at night","model":"gemini-nano-banana-2.1","size":"2K@16:9","n":2}
EOF

bash scripts/run.sh generate-batch \
  --input tmp/jobs.jsonl \
  --out-dir output/imagegen/batch \
  --concurrency 5
```

| 参数 | 说明 |
| --- | --- |
| `--input` | **必填**，JSONL 路径 |
| `--out-dir` | **必填**，输出目录 |
| `--concurrency` | 并发数 1–25 |
| `--max-attempts` | 单个任务重试次数 1–10 |
| `--fail-fast` | 首个失败即中止 |

每个任务可以覆盖 `model`、`size`、`n`、`quality`、`out` 等字段；gemini 任务还可用 `aspect_ratio` + `image_size` 代替 `size`。

### `models` 模型目录

```bash
bash scripts/run.sh models            # 人类可读表格 + 当前 Key 可达清单
bash scripts/run.sh models --json     # 机器可读
bash scripts/run.sh models --refresh  # 立即重探 Key 可达性（默认缓存 6 小时）
```

## 任务类型 `--task` 怎么选

| `--task` | 用在哪 | 通常落到 |
| --- | --- | --- |
| `auto` | 默认；有 `--image` 就视为 `edit`，否则 `general` | 视情况 |
| `draft` | 快速草稿、批量试稿 | 最便宜的文生图 |
| `general` | 通用图 | `seedream-5.0-flash` |
| `photo` | 产品图、写实照 | 同上或更高质量档 |
| `illustration` | 插画、风格化 | 同上 |
| `text` | 图内要有清晰文字（尤其中文） | `gemini-nano-banana-2.1` / `gpt-image-2.5` |
| `edit` | 改图 | 便宜的编辑模型 |
| `cutout` | 去背景要透明底 | `image-cutout`（¥0.02） |
| `upscale` | 放大 2–4× | `image-hd`（¥0.06） |
| `layers` | 拆分图层 | `seedream-5.0-pro-layers`（按层计费） |
| `4k` | 要 4K | `gpt-image-2.5` / `gemini-nano-banana-2.1` / `wan2.7-image-pro` |

`--profile` 与 `--task` 是正交的两个旋钮：

- `--profile cheapest`（默认）：只认钱。
- `--profile balanced`：要求层级 ≥ 高，再在同类里选最便宜 → 通常是 `gemini-nano-banana-2.1`。
- `--profile best`：在最高层级里选最便宜。

## 尺寸规则 `--size`

接受 `WIDTHxHEIGHT`（`WIDTH*HEIGHT` 也行）、模型支持的关键字（`2K`/`4K`）、以及模型允许时的 `auto`。

| 模型 | 规则 |
| --- | --- |
| `seedream-5.0-*` | `2K` 或 ≥ 921,600 总像素的 `WxH`（`1024x1024` ✅，`512x512` ❌）；**不接受 `auto`** |
| `gpt-image-2.5` | 两边都是 16 的倍数，最长边 ≤ 3840，宽高比 ≤ 3:1，总像素 655,360–8,294,400 |
| `gemini-nano-banana-2.1` | 按「档位 + 比例」：`1K`/`2K`/`4K`、`16:9`、`2K@16:9`，或 `WxH`（会被吸附到最近的档位和比例）。返回的是档位尺寸，**不等于**你写的像素 |
| `qwen-image-2.0*` | 每边 512–2048 |
| `image-hd` | 输入长边 ≤ 2048 |
| 其它开放模型 | 每边 256–4096，比例 ≤ 4:1 |

常用尺寸：`1024x1024`、`1536x1024`、`1024x1536`、`2048x2048`、`2048x1152`、`3840x2160`、`2160x3840`。

**`edit` 的 `--size` 特别注意**

- `--size auto` 在 edit 上会被本地拒绝（接口不支持），不会浪费一次请求。
- **不传 `--size` ≠ 保持原图分辨率**：参数会被省略，由中转服务决定分辨率——保持宽高比但可能缩放。实测 `seedream-5.0-flash` 输入 `1024x1536` 返回 `1664x2496`。要求精确尺寸就显式传 `--size`，事后用 `Image.open(p).size` 校验。

## 省钱的正确姿势

1. **先 `--dry-run`**：不发请求、不花钱，能看到解析出的模型和完整请求体。
2. **用 `--task` 而不是硬选贵模型**：抠图 ¥0.02、放大 ¥0.06，比通用模型便宜 5 倍。
3. **设定硬上限**：`--max-cost 0.2` 让解析器拒绝超过 ¥0.2 的方案。
4. **扁平计价模型尺寸免费**：`seedream`/`qwen-image`/`wan`/`z-image`/`image-cutout`/`image-hd` 按张一口价，加大尺寸、提高 quality 不加钱；`gpt-image-2.5` 和 `gemini-nano-banana-2.1` 按 token 计费，越大越贵。
5. **`--n 4` 就是 4 倍成本**，单图模型会被扇出成四次请求。
6. **别为了测速反复跑同一个模型**：每次请求的耗时 CLI 都会打到 stderr（`Generation completed in 12.4s.`），把 stderr 存日志再统计即可（`2>&1 | tee run.log`）。

## 常用配方

```bash
GEN="bash scripts/run.sh"

# 博客头图（2K 横图，质量优先）
$GEN generate --prompt "A polished hero image of a matte ceramic mug on a stone surface" \
  --task photo --profile balanced --size 2048x1152 --out output/imagegen/mug-hero.png

# 商品透明底抠图（¥0.02）
$GEN generate --task cutout --image product.png --cutout-scene product \
  --out output/imagegen/product-cutout.png

# 老图放大 2×
$GEN generate --task upscale --image small.png --upscale-multiple 2 \
  --out output/imagegen/small-hd.png

# 只换背景，保留主体（编辑务必重复不变量）
$GEN edit --image portrait.png \
  --prompt "Replace only the background with a warm sunset; keep the subject, pose and clothing unchanged" \
  --out output/imagegen/portrait-sunset.png \
  --size 1024x1536

# 多张参考图融合成一张（gemini 最擅长）
$GEN generate --image style1.png --image style2.png --image subject.png \
  --model gemini-nano-banana-2.1 --size 2K@16:9 \
  --prompt "Use Image 1 for color palette, Image 2 for lighting, Image 3 as the subject; unify into one scene" \
  --out output/imagegen/fusion.png

# 图内中文海报（文字类选 text 任务）
$GEN generate --task text --size 1024x1536 \
  --prompt "Minimal poster, large headline text: '春日上新', small subline: '2026 春夏系列'" \
  --out output/imagegen/poster.png

# 要 4K
$GEN generate --task 4k --size 3840x2160 --prompt "A vast desert at sunrise" --out output/imagegen/4k.png

# 结构化字段（比堆 prompt 稳）
$GEN generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case product-mockup --style "clean product photography" \
  --composition "wide shot with negative space for copy" \
  --constraints "no logos, no text" \
  --out output/imagegen/mug.png

# 生成 + 同时产出一份网页用缩略图
$GEN generate --prompt "A cozy alpine cabin at dawn" \
  --size 2048x1152 --downscale-max-dim 1280 \
  --out output/imagegen/cabin.png

# 只看会发什么（免费）
$GEN generate --prompt "test" --task text --size 2048x2048 --out /tmp/t.png --dry-run
```

## 退出码与错误处理

- 成功 `0`；参数/配置错误非 `0`，并且**不会**发出请求（例如 seedream 不支持的 `--size auto`、edit 传 `--moderation`、`--mask` 给 gemini）。
- 目标文件已存在会失败，除非 `--force`。
- 中转或网络的瞬时错误，`generate-batch` 会用 `--max-attempts` 重试。
- 找不到可用 Python 时会打印三种修复方式（`uv` 自举 / `ACCTOKEN_IMAGEGEN_VENV` / `ACCTOKEN_IMAGEGEN_PYTHON`）后退出，不会用半残环境硬跑。

## 常见问题排查

| 现象 | 原因 / 解决 |
| --- | --- |
| `No API key found` | 检查上面 7 步的查找顺序；`ACCTOKEN_IMAGE_ENV` 一旦设置会屏蔽其它来源 |
| `reach=NO (key limit)` | 你的 Key 没有该模型权限，换模型或找中转开通；`models --refresh` 重探 |
| 报 `size` 不合法 | seedream 需 ≥ 921,600 px；gpt-image 需 16 的倍数；gemini 用档位而不是任意像素 |
| edit 输出尺寸和原图不同 | 正常，见 [尺寸规则](#尺寸规则---size)；要精确就显式 `--size` |
| 中文文字糊/错字 | 换 `--task text`，落到 `gemini-nano-banana-2.1` 或 `gpt-image-2.5`，并把文字用引号原样写进 prompt |
| 请求超时（120 s） | 4K / 多参考图任务更慢，放后台跑 |
| `--mask` 报错 | 蒙版需 alpha 通道、与首图同尺寸同格式、< 50 MB；gemini 完全不支持 mask |
| 输出带 SynthID / C2PA | `gemini-nano-banana-2.1` 强制水印与内容凭据，无法关闭 |

## 安全说明

- **仓库不含任何 API Key**。Key 只在运行时从环境变量或 `~/.config/acctoken-imagegen.env` 读取。
- `.gitignore` 屏蔽 `*.env`、`*.pem`、`*.key`、`.venv/`、`output/`、`*.log`。
- 仓库自带 `pre-commit` 钩子（`scripts/git-hooks/pre-commit`），会拦截 `.env` 类文件和疑似密钥内容；启用：

  ```bash
  cp scripts/git-hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
  ```

- 配置 Key 的文件请 `chmod 600`；不要把 Key 贴进 prompt、日志、issue 或截图。
- 出图会发送到第三方中转（Acctoken）及其上游模型厂商，**不要上传机密或涉及他人隐私的素材**。

## 仓库结构

```
acctoken-imagegen/
├── SKILL.md                  # Agent 读取的技能说明（何时用、怎么用、成本策略）
├── README.md                 # 本文件（中文详细说明）
├── README.en.md              # English overview
├── LICENSE                   # MIT
├── agents/openai.yaml        # 部分 Agent 的接口描述
├── references/
│   ├── models.md             # 生成的模型价格目录
│   ├── cli.md                # 完整 CLI 参数与默认值
│   ├── image-api.md          # 接口参数、尺寸与各模型限制
│   ├── prompting.md          # prompt 结构与迭代方法
│   ├── sample-prompts.md     # 按场景分类的 prompt 配方
│   └── network-environments.md
└── scripts/
    ├── run.sh                # 入口：解析解释器 → 调 image_gen.py
    ├── _resolve_python.sh    # 解释器探测与自举
    ├── image_gen.py          # 主实现
    ├── model_catalog.py      # 模型/价格/能力目录 + 解析器
    ├── update_pricing.sh     # 抓公开 /api/pricing 刷新目录
    ├── update_pricing.py
    ├── run_chroma.sh         # 绿幕图转透明
    ├── remove_chroma_key.py
    └── git-hooks/pre-commit  # 防密钥入库
```

## 更新价格表

目录价格来自中转的**公开** `/api/pricing`，刷新不需要 Key：

```bash
bash scripts/update_pricing.sh        # 重新生成 references/models.md 与目录快照
bash scripts/run.sh models --refresh  # 重探当前 Key 的可达模型（默认缓存 6h）
```

## License

[MIT](LICENSE)。本项目仅为第三方客户端工具，与 Acctoken、OpenAI、Google、字节跳动、
阿里云等模型提供方无隶属或背书关系；各模型名称与商标归其各自权利人所有。
