# Network / sandbox notes for live calls

Read this when a `scripts/run.sh` call fails to reach the Acctoken endpoint, or when the
agent asks for approval before running it. Nothing here is specific to one agent: each
harness has its own sandbox and approval model, so the pattern is always the same —
**the CLI needs outbound HTTPS, and the sandbox must allow it.**

## Why a live call may need approval
`generate`, `edit`, and `generate-batch` POST to `https://www.acctoken.com/v1` and then
download the returned image URLs. That is outbound network access, which many agent
sandboxes deny or gate behind approval by default. `--dry-run` needs no network at all —
use it to validate a prompt or flag combination before spending a real request.

Self-healing behaviour also touches the network: if no interpreter can import
`openai` / `pillow` / `httpx`, `scripts/run.sh` may run `uv venv` + `uv pip install`. If
you want no install-at-run-time, set `ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP=1` and prepare the
environment explicitly.

## Approvals are not the same thing as network access
- Suppressing approval prompts does **not** grant network access.
- Network access usually lives in the sandbox config, separately from the approval policy.
- So a command can be "approved" and still fail to connect, or be denied before it ever
  tries to connect. Check both knobs when debugging.

## Common configurations

### Codex (`~/.codex/config.toml`)
```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

### Pi
Pi runs commands through the same shell the user has; network reachability is the host's,
not a per-session sandbox flag. If a sandboxed or containerized Pi setup is used (see Pi's
containerization docs), allow outbound HTTPS to `www.acctoken.com` there.

### CI / containers
Grant egress to the relay host, and pre-build the environment so no install happens at
run time:

```bash
uv venv --relocatable "$HOME/.agents/venvs/acctoken-imagegen"
uv pip install --python "$HOME/.agents/venvs/acctoken-imagegen/bin/python" openai pillow httpx
export ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP=1
```

## Deciding what to do
1. Reproduce with `--dry-run`: if that succeeds, the request shape is fine and the problem
   is network or approval, not the prompt or flags.
2. Ask the user before widening a sandbox or lowering an approval policy — that is a
   security-relevant change to their environment, not a convenience setting.
3. Prefer one-off, scoped permission for the single command over a permanent global change.

## Safety note
Enabling network and reducing approvals lowers friction but increases risk when running
untrusted code or working in an untrusted repository. Keep the API key in the config file
outside the skill directory, and never echo it into logs.
