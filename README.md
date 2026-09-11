# Cua E2E Test Agent

An agent that plans and executes one high-value, visible application journey for
a pull request. It reads PR context, uploads a filtered checkout to a disposable
Cua desktop sandbox, drives the running application, and produces a verdict,
report, and screen recording when capture is available.

This is not a wrapper around your existing test suite. The default prompts ask
for direct application interaction and require observable evidence for a pass.

**Release status:** initial release under validation. The example uses `main`
as a development reference; replace it with a reviewed full commit SHA before
supplying secrets.

## Requirements

- Linux GitHub-hosted runner; one agent invocation at a time per runner.
- Nix with flakes enabled. The flake pins nixpkgs and Claude Agent SDK dependencies.
- An existing Cua desktop pool accessible using your Cua client credentials.
  The sandbox must expose cua-driver MCP, computer-server, and a shell, with
  Chromium, a visible X11 desktop, Git, and tar available.
- An Anthropic API key, or a compatible endpoint and model supported by the
  Claude Agent SDK. This is not a provider-independent model adapter.
- A GitHub token able to read the target repository and its pull requests.
- A trusted same-repository PR, explicitly approved by a maintainer for execution.

The project does not grant access to a shared sandbox pool or model gateway and
does not provision infrastructure. Sandbox and model usage can incur charges.

## GitHub Actions

Use `examples/e2e.yml` as the caller workflow, with the action pinned to a reviewed
commit. Configure these repository secrets and variables:

| Name | Kind | Purpose |
| --- | --- | --- |
| `E2E_MODEL_API_KEY` | Secret | Model API credential |
| `CUA_CLIENT_ID` | Secret | Cua OAuth client ID |
| `CUA_CLIENT_SECRET` | Secret | Cua OAuth client secret |
| `E2E_MODEL` | Variable | Model identifier |
| `CUA_E2E_POOL` | Variable | Existing pool name |
| `E2E_MODEL_BASE_URL` | Optional variable | Anthropic-compatible endpoint |

Add the `e2e-test` label to a reviewed same-repository PR. Remove and re-add the
label to rerun. The caller checks out the exact PR SHA without persisting Git
credentials. The agent checks the checkout SHA and current PR metadata before
allocating a sandbox; changed heads and fork PRs fail closed.

The action builds its package from its own action directory, not from the target
checkout. Repository-specific preparation remains in the caller. Only tracked
files are uploaded: generated files needed by the application must be explicitly
included in the index by a trusted preparation step. Untracked files, symlinks,
common credential paths, private key files, and Git metadata are excluded. The
uncompressed snapshot has a 512 MiB limit. Submodules and Git LFS materialization
are not automatically handled.

### Inputs

See `action.yml` for the complete interface. Required inputs are `pr-number`,
`head-sha`, `repo-path`, `model`, `anthropic-api-key`, `cua-client-id`,
`cua-client-secret`, and `pool`. `repository` and `github-token` default to the
caller context. `repo-path` must be an absolute path to the target checkout.

Optional inputs include `planning-model`, `video-editing-model`,
`anthropic-base-url`, `cua-base-url`, `cua-token-url`,
`planning-prompt`, `execution-prompt`, `video-editing-prompt`, and `edit-video`.
Prompt overrides must be trusted absolute paths. Video editing is disabled by
default and consumes additional model time when enabled. Phase model overrides
default to `model` when omitted. The default artifact
name combines the job name and run attempt; override `artifact-name` when a
workflow contains multiple invocations or matrix jobs with the same job ID.

### Outputs And Failure Handling

- `status`: `pass`, `fail`, or `error`. Only `pass` succeeds the action.
- `results-dir`: runner-local report directory for caller-side integrations.
- `artifact-url`: GitHub artifact URL, subject to repository access controls.

The artifact contains the plan, report, model result, runner status, and available
recordings. Raw SDK logs, PR context, credentials, and source archives are not
uploaded. Artifacts are retained for seven days. Treat reports and recordings as
potentially sensitive; do not publish them blindly.

The model result keeps the schema `{"status":"pass|fail","summary":"..."}`.
`runner-status.json` separately reports orchestration and cleanup failures. A
passing model result with failed cleanup yields action status `error`, not pass.

Cleanup attempts claim deletion even after partial creation/binding failures and
stops recorder/proxy processes. Phase timeouts terminate their process groups.
A bounded, redacted stderr diagnostic is preserved for failed or timed-out phases. Claims carry a 120-minute lease as a backstop if
the runner disappears. Recording/editing failures are nonfatal to the journey;
claim cleanup failures are fatal. Cancellation and hard runner termination are
best-effort cleanup scenarios, not guaranteed immediate sandbox deletion.

There is no mandatory AWS, S3, private telemetry, or secret-store integration.
Callers can publish selected artifacts or post a PR comment with their own
credentials. The CLI retains an optional `publish-media` command, which requires
the AWS CLI and your own bucket/configuration; the action never calls it.

## Standalone CLI

From an audited checkout:

```bash
nix run . -- --help
nix build .
./result/bin/e2e-test-agent-action
```

The last command requires configuration; it does not launch an unconfigured run.
Set `E2E_REPOSITORY`, `PR_NUMBER`, `HEAD_SHA`, `E2E_REPO_DIR`, `E2E_WORK_DIR`,
`E2E_POOL`, `GH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`, `CUA_CLIENT_ID`, and
`CUA_CLIENT_SECRET`. Use a fresh work directory outside the target checkout.
Optional environment variables mirror the endpoint and prompt inputs listed
above; video editing uses `E2E_EDIT_VIDEO=true`. Phase model overrides use
`E2E_PLANNING_MODEL` and `E2E_VIDEO_EDITING_MODEL`.

The runner separates model credentials from GitHub/Cua credentials in child
processes and uses an isolated home directory. It does not forward arbitrary
runner environment variables to the agent. Callers may explicitly set
`CLAUDE_CODE_EFFORT_LEVEL` and opt into SDK metrics using
`CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_METRICS_EXPORTER`,
`OTEL_EXPORTER_OTLP_PROTOCOL`, and `OTEL_EXPORTER_OTLP_ENDPOINT`. Only model phases
receive those settings; no collector is configured by this project.
Lower-level CLI commands can be used
for individual phases; `--help` describes their arguments. Command defaults find
bundled prompts without requiring exported prompt paths.

For uncommitted local development, use `nix build path:.` so Nix includes new
files. A normal Git-backed flake includes files known to Git only.

## Development

The unit tests run without model credentials or the Claude Agent SDK installed:

```bash
python3 -m unittest discover -s tests -v
python3 agent/e2e_test_agent.py --help
nix flake check
```

Live validation additionally requires the configured model endpoint and Cua pool.
Mocked unit tests are not evidence of a successful real sandbox run.

## Security And License

Licensed under the Apache License, Version 2.0. See `LICENSE` for the full terms.
Read `SECURITY.md` before enabling the workflow.
