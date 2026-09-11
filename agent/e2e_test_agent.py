#!/usr/bin/env python3

import argparse
import dataclasses
import functools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any


PROMPT_TOKEN = re.compile(r"{{([A-Z0-9_]+)}}")
CONTEXT_FILES = (
    "pr-details.json",
    "pr-diff.patch",
    "changed-files.txt",
    "file-stats.txt",
    "commits.txt",
)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def repository_name(value: str | None = None) -> str:
    repository = value if value is not None else env("E2E_REPOSITORY", env("GITHUB_REPOSITORY"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ValueError("Set repository as owner/name via E2E_REPOSITORY or GITHUB_REPOSITORY")
    return repository


def github_error(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)


def github_warning(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


def render_prompt(path: Path, values: dict[str, str]) -> str:
    template = path.read_text()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"Prompt {path} references unknown token {key}")
        return values[key]

    rendered = PROMPT_TOKEN.sub(replace, template)
    unresolved = PROMPT_TOKEN.findall(rendered)
    if unresolved:
        raise ValueError(f"Prompt {path} has unresolved tokens: {unresolved}")
    return rendered


def message_json(message: Any) -> str:
    if dataclasses.is_dataclass(message):
        value = dataclasses.asdict(message)
    else:
        value = repr(message)
    return json.dumps(value, default=str, separators=(",", ":"))


def path_is_within(root: Path, value: str) -> bool:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:  # lint-ignore: swallowed-exception - predicate: path outside root
        return False
    return True


def planning_permission_hook(root: Path):
    async def guard(input_data, _tool_use_id, _context):
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input", {})
        reason = ""

        if tool_name == "Write":
            file_path = tool_input.get("file_path", "")
            if not file_path or Path(file_path).name != "e2e-plan.md":
                reason = "Planning may only write e2e-plan.md"
            elif not path_is_within(root, file_path):
                reason = "Planning writes must remain in the planning directory"
        elif tool_name == "Read":
            file_path = tool_input.get("file_path", "")
            if not file_path or not path_is_within(root, file_path):
                reason = "Planning reads must remain in the planning directory"
        elif tool_name == "Bash":
            allowed = video_command_is_allowed(
                root, tool_input.get("command", "")
            )
        elif tool_name in {"Glob", "Grep"}:
            search_path = tool_input.get("path", ".")
            if not path_is_within(root, search_path):
                reason = "Planning searches must remain in the planning directory"
            if tool_name == "Glob":
                pattern = tool_input.get("pattern", "")
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    reason = "Planning glob patterns may not escape the planning directory"
        else:
            reason = f"Tool {tool_name} is not available during planning"

        if not reason:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return guard


def video_command_is_allowed(root: Path, command: str) -> bool:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # lint-ignore: swallowed-exception - predicate: unparseable command
        return False
    if not tokens or any(token in {";", "&&", "||", "|", ">", ">>", "<", "<<", "(", ")"} for token in tokens):
        return False
    executable = tokens[0]
    if executable in {"python", "python3"}:
        if len(tokens) < 2 or Path(tokens[1]).name != "edit_video.py":
            return False
    elif executable not in {"ffmpeg", "ffprobe", "melt"}:
        return False
    for token in tokens[1:]:
        candidate = Path(token)
        if candidate.is_absolute() and not path_is_within(root, token):
            return False
        if "/" in token and ".." in candidate.parts:
            return False
    return True


def workspace_permission_hook(root: Path):
    async def guard(input_data, _tool_use_id, _context):
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input", {})
        if tool_name in {"Read", "Write", "Edit"}:
            file_path = tool_input.get("file_path", "")
            allowed = bool(file_path) and path_is_within(root, file_path)
        elif tool_name in {"Glob", "Grep"}:
            allowed = path_is_within(root, tool_input.get("path", "."))
            if tool_name == "Glob":
                pattern = tool_input.get("pattern", "")
                allowed = allowed and not Path(pattern).is_absolute()
                allowed = allowed and ".." not in Path(pattern).parts
        else:
            allowed = True
        if allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "File tools must remain in the configured working directory"
                ),
            }
        }

    return guard


async def run_agent(
    *,
    prompt: str,
    cwd: Path,
    allowed_tools: list[str],
    mcp_servers: dict[str, dict[str, str]] | None = None,
    planning_root: Path | None = None,
    workspace_root: Path | None = None,
    tools: list[str] | None = None,
    max_turns: int = 64,
    sandbox: dict[str, Any] | None = None,
) -> bool:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKError,
        HookMatcher,
        ResultMessage,
        query,
    )

    hooks = {}
    if planning_root:
        hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher="Read|Glob|Grep|Write",
                    hooks=[planning_permission_hook(planning_root)],
                )
            ]
        }
    elif workspace_root:
        hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher="Bash|Read|Glob|Grep|Write|Edit",
                    hooks=[workspace_permission_hook(workspace_root)],
                )
            ]
        }
    options = ClaudeAgentOptions(
        allowed_tools=allowed_tools,
        cwd=cwd,
        env={"CLAUDE_AGENT_SDK_CLIENT_APP": "cua-e2e-test-agent/1.0"},
        hooks=hooks,
        # A cua-driver vision capture returns one JSON message carrying a
        # base64 screenshot, which overflows the SDK's 1 MiB default and kills
        # the whole session ("JSON message exceeded maximum buffer size").
        max_buffer_size=32 * 1024 * 1024,
        max_turns=max_turns,
        mcp_servers=mcp_servers or {},
        model=env("ANTHROPIC_MODEL") or None,
        permission_mode="dontAsk",
        sandbox=sandbox,
        setting_sources=[],
        strict_mcp_config=bool(mcp_servers),
        system_prompt={"type": "preset", "preset": "claude_code"},
        tools=tools or ["Read", "Glob", "Grep", "Write"],
    )
    result: ResultMessage | None = None
    log_path = Path(env("CLAUDE_OUTPUT_LOG", "/tmp/claude-output.json"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        try:
            async for message in query(prompt=prompt, options=options):
                line = message_json(message)
                print(line, flush=True)
                log.write(line + "\n")
                log.flush()
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as error:  # lint-ignore: swallowed-exception - logged below
            line = json.dumps({"sdk_error": str(error)}, separators=(",", ":"))
            print(line, file=sys.stderr, flush=True)
            log.write(line + "\n")
            return False
    return result is not None and not result.is_error


def run_agent_sync(**kwargs: Any) -> bool:
    import anyio

    return anyio.run(functools.partial(run_agent, **kwargs))


def prompt_values(args: argparse.Namespace) -> dict[str, str]:
    repo_ready = args.repo_ready.lower() == "true"
    return {
        "REPOSITORY": args.repository,
        "PR_NUMBER": args.pr_number,
        "PR_TITLE": args.pr_title,
        "HEAD_REF": args.head_ref,
        "BASE_REF": args.base_ref,
        "HEAD_SHA": args.head_sha,
        "PR_AUTHOR": args.pr_author,
        "SANDBOX_REPO_DIR": args.sandbox_repo_dir,
        "REPO_STATUS": (
            "READY"
            if repo_ready
            else "FAILED - report the missing repository snapshot; do not attempt an authenticated clone"
        ),
    }


def is_nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def run_planning(args: argparse.Namespace) -> int:
    planning_dir = args.planning_dir.resolve()
    shutil.rmtree(planning_dir, ignore_errors=True)
    planning_dir.mkdir(parents=True)
    for name in CONTEXT_FILES:
        source = args.context_dir / name
        if not source.is_file():
            github_error(f"Missing planning context file: {source}")
            return 1
        shutil.copy2(source, planning_dir / name)

    prompt = render_prompt(args.planning_prompt, prompt_values(args))
    target = planning_dir / "e2e-plan.md"

    for attempt in range(1, 3):
        shutil.rmtree(planning_dir / ".claude", ignore_errors=True)
        target.unlink(missing_ok=True)
        print(f"Starting E2E planning attempt {attempt} of 2...")
        success = run_agent_sync(
            prompt=prompt,
            cwd=planning_dir,
            allowed_tools=["Read", "Glob", "Grep", "Write"],
            planning_root=planning_dir,
        )
        if success and is_nonempty(target):
            break
        github_warning(f"E2E planning attempt {attempt} failed")
        if attempt < 2:
            time.sleep(attempt * 5)
    else:
        github_error("Planning phase completed without a non-empty e2e-plan.md")
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "e2e-plan.md"
    shutil.copy2(target, output)
    print(f"E2E plan created: {output.stat().st_size} bytes")
    return 0


def valid_result(path: Path) -> bool:
    try:
        result = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):  # lint-ignore: swallowed-exception - predicate: unreadable result
        return False
    return (
        isinstance(result, dict)
        and result.get("status") in {"pass", "fail"}
        and isinstance(result.get("summary"), str)
        and bool(result["summary"].strip())
    )


def run_execution(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plan = args.output_dir / "e2e-plan.md"
    if not is_nonempty(plan):
        github_error(f"Missing E2E plan: {plan}")
        return 1

    prompt = render_prompt(args.execution_prompt, prompt_values(args))
    report = args.output_dir / "e2e-report.md"
    result = args.output_dir / "e2e-result.json"
    mcp_servers = {
        "cua-driver": {"type": "http", "url": f"{args.mcp_base_url}/mcp"},
        "sandbox-shell": {
            "type": "http",
            "url": f"{args.mcp_base_url}/shell/mcp",
        },
    }

    for attempt in range(1, 3):
        report.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
        print(f"Starting E2E execution attempt {attempt} of 2...")
        attempt_prompt = prompt
        if attempt == 2:
            attempt_prompt += """

RETRY CONTEXT:
- The disposable sandbox persists from attempt 1. Reuse any installed dependencies, downloaded Keycloak,
  patched runtime config, fixtures, dev servers, and visible browser already present.
- Do not repeat source analysis or rebuild setup from scratch. Inspect existing processes and logs once in a
  single batched shell call, repair only the failed component, and proceed directly to the GUI journey.
- You have one recovery attempt. If it remains blocked, immediately write e2e-report.md and e2e-result.json.
"""
        success = run_agent_sync(
            prompt=attempt_prompt,
            cwd=args.output_dir,
            allowed_tools=[
                "Read",
                "Glob",
                "Grep",
                "Write",
                "mcp__cua-driver",
                "mcp__sandbox-shell__shell_execute",
            ],
            mcp_servers=mcp_servers,
            workspace_root=args.output_dir,
        )
        if success and valid_result(result):
            print(f"E2E result: {result.read_text().strip()}")
            return 0
        github_warning(f"E2E execution attempt {attempt} failed")
        if attempt < 2:
            time.sleep(attempt * 5)

    github_error("E2E execution agent did not produce a valid e2e-result.json")
    return 1


def copy_if_present(source: Path, destination: Path) -> None:
    if source.is_file():
        shutil.copy2(source, destination)


def sanitized_summary(source: Path, destination: Path) -> None:
    if not is_nonempty(source):
        return
    line = source.read_text(errors="replace")[:1000].replace("\r", "").splitlines()
    if line:
        destination.write_text(line[0] + "\n")


def validate_edited_video(args: argparse.Namespace, edited: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(args.video_editor),
            "--input",
            str(args.output_dir / "e2e-video.mp4"),
            "--validate-edited",
            str(edited),
        ],
        check=False,
    )
    return result.returncode == 0


def scripted_video_edit(args: argparse.Namespace) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(args.video_editor),
            "--input",
            str(args.output_dir / "e2e-video.mp4"),
            "--output",
            str(args.output_dir / "e2e-video-edited.mp4"),
            "--project",
            str(args.output_dir / "e2e-video.mlt"),
            "--summary",
            str(args.video_summary),
        ],
        check=False,
    )
    return result.returncode == 0


def run_video_edit(args: argparse.Namespace) -> int:
    raw_video = args.output_dir / "e2e-video.mp4"
    if not is_nonempty(raw_video):
        github_warning("Video edit skipped because e2e-video.mp4 is unavailable")
        return 0

    edit_dir = args.video_edit_dir.resolve()
    shutil.rmtree(edit_dir, ignore_errors=True)
    edit_dir.mkdir(parents=True)
    shutil.copy2(raw_video, edit_dir / "e2e-video.mp4")
    shutil.copy2(args.video_editor, edit_dir / "edit_video.py")
    copy_if_present(args.output_dir / "e2e-plan.md", edit_dir / "e2e-plan.md")
    copy_if_present(args.output_dir / "e2e-report.md", edit_dir / "e2e-report.md")

    prompt = render_prompt(args.video_editing_prompt, prompt_values(args))
    edited = edit_dir / "e2e-video-edited.mp4"
    project = edit_dir / "e2e-video.mlt"
    summary = edit_dir / "e2e-video-edit-summary.txt"
    success = run_agent_sync(
        prompt=prompt,
        cwd=edit_dir,
        allowed_tools=["Bash", "Read", "Glob", "Grep", "Write", "Edit"],
        workspace_root=edit_dir,
        tools=["Bash", "Read", "Glob", "Grep", "Write", "Edit"],
        max_turns=32,
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        },
    )

    if success and is_nonempty(edited) and validate_edited_video(args, edited):
        shutil.copy2(edited, args.output_dir / edited.name)
        copy_if_present(project, args.output_dir / project.name)
        sanitized_summary(summary, args.video_summary)
        print("Agentic video edit accepted")
        return 0
    if success and not edited.exists() and is_nonempty(summary):
        sanitized_summary(summary, args.video_summary)
        print(f"Agent skipped the edit: {summary.read_text()[:500]}")
        return 0

    github_warning("Agentic edit failed; falling back to scripted dead-air editing")
    if scripted_video_edit(args):
        if is_nonempty(args.output_dir / "e2e-video-edited.mp4"):
            print("Scripted fallback video edit accepted")
        else:
            args.video_summary.unlink(missing_ok=True)
            print("Scripted fallback skipped the edit; publishing the raw recording")
        return 0

    github_warning("Scripted video edit failed; publishing the raw recording")
    for output in ("e2e-video-edited.mp4", "e2e-video.mlt"):
        (args.output_dir / output).unlink(missing_ok=True)
    args.video_summary.unlink(missing_ok=True)
    return 0





import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import shlex
import shutil
import signal
import subprocess
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_TOKEN_URL = "https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token"
DEFAULT_BASE_URL = "https://run.cua.ai"
CLAIM_GROUP, CLAIM_VERSION, CLAIM_PLURAL = "osgym.cua.ai", "v1alpha1", "osgymsandboxclaims"
SHELL_TOOL_CANDIDATES = ("shell_execute", "run_command", "bash")
RECORDING_PATH = "/tmp/e2e-recording.mp4"
CHUNK_BYTES = 2 * 1024 * 1024
MAX_CHUNKS = 100  # 200 MB ceiling on the ffmpeg-fallback download

def log(msg: str) -> None:
    print(f"[cua-sandbox] {msg}", file=sys.stderr, flush=True)


def base_url() -> str:
    return os.environ.get("CUA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


class TokenSource:
    """Client-credentials token cache; refreshes when <120s of life remains."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._expiry = 0.0

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expiry - 120:
                return self._token
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": os.environ["CUA_CLIENT_ID"],
                "client_secret": os.environ["CUA_CLIENT_SECRET"],
            }).encode()
            req = urllib.request.Request(
                os.environ.get("CUA_TOKEN_URL", DEFAULT_TOKEN_URL),
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
            self._token = payload["access_token"]
            self._expiry = time.time() + int(payload.get("expires_in", 300))
            return self._token


TOKENS = TokenSource()


def api_request(method: str, path: str, body=None, headers=None, timeout=60):
    """One authenticated request against the control plane. Returns the response
    object (caller reads it); raises urllib.error.HTTPError on non-2xx."""
    req = urllib.request.Request(base_url() + path, data=body, method=method)
    req.add_header("Authorization", f"Bearer {TOKENS.get()}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urllib.request.urlopen(req, timeout=timeout)


def claims_path(pool: str, name: str = "") -> str:
    path = f"/api/k8s/apis/{CLAIM_GROUP}/{CLAIM_VERSION}/namespaces/{pool}/{CLAIM_PLURAL}"
    return f"{path}/{name}" if name else path


def sse_json(text: str) -> dict:
    line = next((ln[6:] for ln in text.splitlines() if ln.startswith("data: ")), text)
    return json.loads(line)


class McpSession:
    """Minimal Streamable-HTTP MCP client for the sandbox's cua-driver endpoint."""

    def __init__(self, pool: str, sandbox: str, service: str):
        self.url_path = f"/api/svc/{pool}/{sandbox}-{service}/mcp"
        self.session_id = None
        self.tools = []

    def _post(self, payload: dict, timeout: int):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return api_request("POST", self.url_path, json.dumps(payload).encode(), headers, timeout)

    def open(self, wait_ready: int = 0) -> None:
        deadline = time.time() + max(wait_ready, 1)
        while True:
            try:
                resp = self._post({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "gha-e2e-agent", "version": "0.1"}},
                }, timeout=30)
                self.session_id = resp.headers.get("mcp-session-id")
                resp.read()
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
                status = getattr(err, "code", None)
                retryable = status in (502, 503, 504) or not isinstance(err, urllib.error.HTTPError)
                if retryable and time.time() < deadline:
                    log(f"sandbox MCP not ready ({err}); retrying")
                    time.sleep(5)
                    continue
                raise
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30).read()
        listing = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, timeout=30)
        self.tools = [t["name"] for t in
                      sse_json(listing.read().decode()).get("result", {}).get("tools", [])]

    def call(self, name: str, arguments: dict, timeout: int = 600) -> tuple[str, bool]:
        resp = self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}}, timeout)
        result = sse_json(resp.read().decode()).get("result", {})
        text = "\n".join(block.get("text", "") for block in result.get("content", [])
                         if block.get("type") == "text")
        return text, bool(result.get("isError"))

    def shell_tool(self) -> str:
        for candidate in SHELL_TOOL_CANDIDATES:
            if candidate in self.tools:
                return candidate
        raise RuntimeError(f"no shell tool on this sandbox; available tools: {sorted(self.tools)}")

    def shell(self, command: str, timeout: int = 600) -> tuple[str, bool]:
        return self.call(self.shell_tool(), {"command": command}, timeout)


# ── claim lifecycle ──────────────────────────────────────────────────────────

def cmd_claim_create(args) -> int:
    shutdown = (datetime.now(timezone.utc)
                + timedelta(minutes=args.lease_minutes)).isoformat()
    manifest = {
        "apiVersion": f"{CLAIM_GROUP}/{CLAIM_VERSION}", "kind": "OSGymSandboxClaim",
        "metadata": {"name": args.name,
                     "annotations": {"cua.ai/workflow-id":
                                     f"gha-{os.environ.get('GITHUB_RUN_ID', 'local')}"}},
        "spec": {"sandboxTemplateRef": {"name": args.template or f"{args.pool}-template"},
                 "bindDeadline": args.bind_deadline,
                 # The reaper honors shutdownTime and nothing else: a claim
                 # without one is never reaped if this job dies before cleanup.
                 "lifecycle": {"shutdownTime": shutdown, "shutdownPolicy": "Retain"}},
    }
    try:
        resp = api_request("POST", claims_path(args.pool), json.dumps(manifest).encode(),
                           {"Content-Type": "application/json"})
        resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 403:
            raise SystemExit("403 creating claim -- the credential must be a per-USER "
                             "key (ukey-...); service/project keys are rejected")
        if err.code != 409:  # already exists: fine on a re-run attempt
            raise
    log(f"claim {args.name} created in pool {args.pool} (shutdownTime={shutdown})")
    return 0


def cmd_claim_wait(args) -> int:
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        with api_request("GET", claims_path(args.pool, args.name)) as resp:
            status = json.loads(resp.read()).get("status") or {}
        phase = status.get("phase", "Pending")
        if phase == "Bound" and (status.get("sandbox") or {}).get("name"):
            print(status["sandbox"]["name"])
            return 0
        if phase in ("Failed", "Error", "Expired"):
            log(f"claim entered terminal phase {phase}: {json.dumps(status)[:2000]}")
            return 1
        time.sleep(5)
    log(f"claim {args.name} not Bound after {args.timeout}s")
    return 1


def cmd_claim_delete(args) -> int:
    try:
        api_request("DELETE", claims_path(args.pool, args.name)).read()
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
    log(f"claim {args.name} released")
    return 0


# ── in-sandbox shell ─────────────────────────────────────────────────────────

def guest_server_path(args, endpoint: str) -> str:
    service = args.sandbox
    server_service = getattr(args, "server_service", "")
    if server_service:
        service = f"{service}-{server_service}"
    return f"/api/svc/{args.pool}/{service}/{endpoint}"


def computer_server_path(args, endpoint: str = "cmd") -> str:
    return f"/api/svc/{args.pool}/{args.sandbox}-server/{endpoint}"


def computer_server_command(args, command: str, params: dict, timeout: int) -> dict:
    request_timeout = min(max(int(timeout), 1), 3600)
    body = json.dumps({"command": command, "params": params}).encode()
    for attempt in range(1, 5):
        try:
            with api_request(
                "POST",
                computer_server_path(args),
                body,
                {"Content-Type": "application/json"},
                timeout=request_timeout + 30,
            ) as response:
                return sse_json(response.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            status = getattr(err, "code", None)
            retryable = status in (502, 503, 504) or not isinstance(err, urllib.error.HTTPError)
            if not retryable or attempt == 4:
                raise
            delay = attempt * 3
            log(f"computer-server transient failure ({err}); retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError("computer-server request exhausted retries")


def guest_execute(args, command: str, timeout: int) -> tuple[str, bool]:
    request_timeout = min(max(int(timeout), 1), 3600)
    result = computer_server_command(
        args,
        "run_command",
        {"command": command, "timeout": request_timeout},
        request_timeout,
    )
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    returncode = int(result.get("return_code", 1))
    if not result.get("success", False) and not stderr:
        stderr = result.get("error") or result.get("message") or "computer-server command failed"
    text = stdout
    if stderr:
        text += ("\n" if text and not text.endswith("\n") else "") + stderr
    text += f"\n[exit_code={returncode}]"
    return text, returncode != 0


def cmd_upload(args) -> int:
    total = os.path.getsize(args.source)
    written = 0
    with open(args.source, "rb") as source:
        append = False
        while chunk := source.read(CHUNK_BYTES):
            result = computer_server_command(
                args,
                "write_bytes",
                {
                    "path": args.destination,
                    "content_b64": base64.b64encode(chunk).decode(),
                    "append": append,
                },
                300,
            )
            if not result.get("success", False):
                raise SystemExit(
                    "computer-server upload failed: "
                    + (result.get("error") or result.get("message") or json.dumps(result)[:500])
                )
            written += len(chunk)
            append = True
    log(f"uploaded {args.source} -> {args.destination} ({written}/{total} bytes)")
    return 0


def screenshot_bytes(result: dict) -> bytes:
    encoded = result.get("image_data")
    if not encoded:
        images = result.get("images") or []
        if images:
            encoded = images[0].get("data_base64")
    if not result.get("success", False) or not encoded:
        raise RuntimeError(
            result.get("error") or result.get("message") or "screenshot returned no image_data"
        )
    return base64.b64decode(encoded)


def cmd_capture_frames(args) -> int:
    os.makedirs(args.output_dir, exist_ok=True)
    frame_index = 1
    while True:
        try:
            result = computer_server_command(
                args,
                "screenshot",
                {"format": "png", "quality": 95},
                60,
            )
            frame = screenshot_bytes(result)
            output = os.path.join(args.output_dir, f"frame-{frame_index:06d}.png")
            with open(output, "wb") as file:
                file.write(frame)
            if frame_index == 1:
                log(f"capturing desktop frames in {args.output_dir}")
            frame_index += 1
        except Exception as err:
            log(f"desktop frame capture failed: {err}")
        time.sleep(args.interval)


def cmd_exec(args) -> int:
    command = args.command if args.command else sys.stdin.read()
    deadline = time.time() + max(args.wait_ready, 1)
    while True:
        try:
            text, is_error = guest_execute(args, command, args.timeout)
            print(text)
            return 1 if is_error else 0
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            status = getattr(err, "code", None)
            retryable = status in (502, 503, 504) or not isinstance(err, urllib.error.HTTPError)
            if retryable and time.time() < deadline:
                log(f"sandbox computer-server not ready ({err}); retrying")
                time.sleep(5)
                continue
            log(f"computer-server shell unavailable ({err}); trying MCP shell")
            break

    session = McpSession(args.pool, args.sandbox, args.service)
    session.open(wait_ready=args.wait_ready)
    text, is_error = session.shell(command, timeout=args.timeout)
    print(text)
    return 1 if is_error else 0


# ── local auth-injecting proxy ───────────────────────────────────────────────

SHELL_MCP_PATH = "/shell/mcp"
SHELL_TOOL = {
    "name": "shell_execute",
    "description": "Run a shell command inside the claimed disposable sandbox.",
    "inputSchema": {
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
        },
        "additionalProperties": False,
    },
}


def shell_mcp_response(payload: dict, args) -> tuple[int, dict | None]:
    request_id = payload.get("id")
    method = payload.get("method")
    if method == "initialize":
        protocol = payload.get("params", {}).get("protocolVersion", "2025-03-26")
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cua-sandbox-shell", "version": "0.1"},
            },
        }
    if method == "notifications/initialized":
        return 202, None
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [SHELL_TOOL]},
        }
    if method == "tools/call":
        params = payload.get("params", {})
        if params.get("name") != SHELL_TOOL["name"]:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "unknown shell tool"},
            }
        arguments = params.get("arguments", {})
        command = arguments.get("command", "")
        if not command:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "command is required"},
            }
        try:
            visible_command = f"export CUA_E2E_HEADED=1\n{command}"
            text, is_error = guest_execute(args, visible_command, arguments.get("timeout", 600))
        except Exception as err:
            text, is_error = f"sandbox shell request failed: {err}", True
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }
    return 200, {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def cmd_proxy(args) -> int:
    svc_base = f"/api/svc/{args.pool}/{args.sandbox}-{args.service}"
    upstream = base_url()
    forward_req_headers = ("Content-Type", "Accept", "Mcp-Session-Id",
                           "Mcp-Protocol-Version", "Last-Event-Id")
    forward_resp_headers = ("Content-Type", "Mcp-Session-Id")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, status: int, payload: dict | None) -> None:
            body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Mcp-Session-Id", "cua-sandbox-shell")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _shell_mcp(self) -> None:
            if self.command != "POST":
                self._send_json(405, {"error": "method not allowed"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) if length else b"{}")
            except json.JSONDecodeError as err:
                self._send_json(400, {"error": f"invalid JSON: {err}"})
                return
            status, response = shell_mcp_response(payload, args)
            self._send_json(status, response)

        def _proxy(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(upstream + svc_base + self.path,
                                         data=body, method=self.command)
            for header in forward_req_headers:
                value = self.headers.get(header)
                if value:
                    req.add_header(header, value)
            req.add_header("Authorization", f"Bearer {TOKENS.get()}")
            timeout = None if self.command == "GET" else 600
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as err:
                resp = err
            except urllib.error.URLError as err:
                self.send_error(502, f"upstream unreachable: {err.reason}")
                return
            try:
                self.send_response(resp.status)
                for header in forward_resp_headers:
                    value = resp.headers.get(header)
                    if value:
                        self.send_header(header, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                resp.close()

        def do_GET(self):
            self._shell_mcp() if self.path == SHELL_MCP_PATH else self._proxy()

        def do_POST(self):
            self._shell_mcp() if self.path == SHELL_MCP_PATH else self._proxy()

        def do_DELETE(self):
            self._shell_mcp() if self.path == SHELL_MCP_PATH else self._proxy()

        def log_message(self, fmt, *values):
            log(f"proxy {self.command} {self.path} -> {fmt % values}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    log(f"proxying 127.0.0.1:{args.port} -> {upstream}{svc_base} with sandbox shell")
    server.serve_forever()
    return 0


# ── screen recording ─────────────────────────────────────────────────────────

FFMPEG_START = f"""
set -e
command -v ffmpeg >/dev/null 2>&1 || {{ echo NO_FFMPEG; exit 0; }}
DISP=$(ls /tmp/.X11-unix 2>/dev/null | sed -n 's/^X/:/p' | head -1); DISP=${{DISP:-:0}}
GEOM=$(xdpyinfo -display "$DISP" 2>/dev/null | awk '/dimensions:/{{print $2}}'); GEOM=${{GEOM:-1280x800}}
nohup ffmpeg -y -f x11grab -draw_mouse 1 -video_size "$GEOM" -framerate 15 -i "$DISP" \\
  -c:v libx264 -preset veryfast -pix_fmt yuv420p {RECORDING_PATH} \\
  >/tmp/e2e-ffmpeg.log 2>&1 &
sleep 2
pgrep -f x11grab >/dev/null && echo RECORDING_STARTED || {{ cat /tmp/e2e-ffmpeg.log; echo RECORDING_FAILED; }}
"""

FFMPEG_STOP = """
pkill -INT -f x11grab 2>/dev/null || true
for _ in $(seq 1 20); do pgrep -f x11grab >/dev/null 2>&1 || break; sleep 1; done
stat -c %s {path} 2>/dev/null || echo 0
""".format(path=RECORDING_PATH)


def install_guest_ffmpeg(args) -> None:
    try:
        session = McpSession(args.pool, args.sandbox, args.service)
        session.open(wait_ready=30)
        if "install_ffmpeg" not in session.tools:
            log("cua-driver does not expose install_ffmpeg")
            return
        text, is_error = session.call("install_ffmpeg", {"confirm": True}, timeout=300)
        if is_error:
            log(f"cua-driver install_ffmpeg failed: {text[:500]}")
        else:
            log("cua-driver install_ffmpeg completed")
    except Exception as err:
        log(f"could not install ffmpeg through cua-driver: {err}")


def cmd_start_recording(args) -> int:
    """Prints the recording method (server|ffmpeg|none) on stdout."""
    try:
        api_request("POST", guest_server_path(args, "start_recording"), b"",
                    {"Content-Type": "application/json"}, timeout=30).read()
        log("recording via guest server /start_recording")
        print("server")
        return 0
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
        log(f"guest server recording unavailable ({err}); trying in-guest ffmpeg")
    install_guest_ffmpeg(args)
    try:
        text, is_error = guest_execute(args, FFMPEG_START, timeout=120)
    except Exception as err:  # recording is best-effort: never fail the run
        log(f"ffmpeg fallback failed to start: {err}")
        print("none")
        return 0
    if not is_error and "RECORDING_STARTED" in text:
        log("recording via in-guest ffmpeg x11grab")
        print("ffmpeg")
    else:
        log(f"ffmpeg fallback unavailable: {text[:500]}")
        print("none")
    return 0


def cmd_stop_recording(args) -> int:
    if args.method == "none":
        log("no recording was started; nothing to download")
        return 0
    if args.method == "server":
        try:
            with api_request("POST", guest_server_path(args, "end_recording"), b"",
                             {"Content-Type": "application/json"}, timeout=300) as resp, \
                    open(args.output, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            log(f"failed to download recording from guest server: {err}")
            return 0
        log(f"recording saved to {args.output} ({os.path.getsize(args.output)} bytes)")
        return 0

    try:
        size_text, _ = guest_execute(args, FFMPEG_STOP, timeout=120)
        size_text = size_text.rsplit("\n[exit_code=", 1)[0]
        size = int(size_text.strip().splitlines()[-1] or 0)
        if size <= 0:
            log("ffmpeg produced no recording file")
            return 0
        with open(args.output, "wb") as out:
            for index in range(min(MAX_CHUNKS, (size + CHUNK_BYTES - 1) // CHUNK_BYTES)):
                chunk_cmd = (f"dd if={RECORDING_PATH} bs={CHUNK_BYTES} "
                             f"skip={index} count=1 2>/dev/null | base64 -w0")
                encoded, is_error = guest_execute(args, chunk_cmd, timeout=300)
                if is_error or not encoded.strip():
                    break
                encoded = encoded.rsplit("\n[exit_code=", 1)[0]
                out.write(base64.b64decode(encoded.strip()))
    except Exception as err:
        log(f"failed to download ffmpeg recording: {err}")
        return 0
    log(f"recording saved to {args.output} ({os.path.getsize(args.output)} bytes)")
    return 0


# ── entrypoint ───────────────────────────────────────────────────────────────

SNAPSHOT_DIR = "/tmp/e2e-repo-files"
PREVIEW_GIF = "e2e-video-preview.gif"
FULL_MP4 = "e2e-video-full.mp4"
VIDEO_URL_FILE = "/tmp/e2e-video-url.txt"
PREVIEW_URL_FILE = "/tmp/e2e-preview-url.txt"
FULL_URL_FILE = "/tmp/e2e-video-full-url.txt"
COMMIT_MARKER = "commit-marker-9f27c41e"

# The staged repo lives in a scratch directory so the outer git checkout stays
# clean; the commit marker makes that explicit in reviews.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "GitHub Actions",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "GitHub Actions",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}


def append_output(key: str, value: str) -> None:
    text = os.environ.get("GITHUB_OUTPUT")
    if text:
        with open(text, "a") as out:
            delimiter = "e2e_" + uuid.uuid4().hex
            out.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    else:
        print(f"{key}={value}")


def run_git(args: list[str], cwd: str | None = None) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    out = subprocess.check_output(["git", *args], cwd=cwd, env=env,
                                  stderr=subprocess.STDOUT).decode()
    return out.rstrip("\n")


def require_gh(token: str) -> None:
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    subprocess.check_call(["gh", "auth", "status"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stage_repo_snapshot(repo_dir: str) -> str:
    """Copy the checked-out repo into SNAPSHOT_DIR. Excludes .git so the outer
    checkout stays clean; the agent works on a plain tree and the commit that
    carries COMMIT_MARKER is made in the sandbox by publish-media's caller."""
    dst = Path(SNAPSHOT_DIR)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(repo_dir, dst, ignore=shutil.ignore_patterns(".git"),
                    symlinks=True)
    (dst / ".e2e-snapshot").write_text(
        "snapshot of the PR head checkout prepared by the E2E Test Agent\n")
    log(f"repository snapshot staged in {dst}")
    return str(dst)


def collect_changed_paths(pr_number: str, token: str) -> frozenset[str]:
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    out = subprocess.check_output(
        ["gh", "pr", "view", pr_number, "--repo", repository_name(), "--json", "files", "--jq", ".files[].path"],
        env=env, text=True).splitlines()
    return frozenset(line.strip() for line in out if line.strip())


def repo_file_list(changed: frozenset[str], max_size: int) -> bytes:
    """NUL-separated repo paths to snapshot: small tracked files plus anything
    the PR touched (even when large)."""
    tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
    out = bytearray()
    for raw in tracked:
        if not raw:
            continue
        path = Path(os.fsdecode(raw))
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        if size <= max_size or path.as_posix() in changed:
            out += raw + b"\0"
    return bytes(out)


def gh_json(pr_number: str, token: str, fields: str, jq: str = "") -> str:
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    args = ["gh", "pr", "view", pr_number, "--repo", repository_name(), "--json", fields]
    if jq:
        args += ["--jq", jq]
    return subprocess.check_output(args, env=env, text=True)


def cmd_provision(args) -> int:
    """Claim a sandbox for this run, staging the snapshot file list first
    when --snapshot is set. claim-wait prints the bound sandbox name."""
    require_gh(args.token)
    if args.snapshot:
        stage_repo_snapshot(args.repo_dir)
        changed = collect_changed_paths(args.pr_number, args.token)
        data = repo_file_list(changed, args.max_size)
        with open("/tmp/e2e-repo-files.zlist", "wb") as out:
            out.write(data)
        log(f"snapshot file list: {len(data)} bytes")
    else:
        if Path("/tmp/e2e-repo-files.zlist").exists():
            Path("/tmp/e2e-repo-files.zlist").unlink()
        log("snapshot disabled; no file list written")
    if cmd_claim_create(SimpleNamespace(
            pool=args.pool, name=args.name, template="",
            lease_minutes=args.lease_minutes,
            bind_deadline=args.bind_deadline)) != 0:
        return 1
    return cmd_claim_wait(SimpleNamespace(pool=args.pool, name=args.name,
                                          timeout=args.bind_deadline))



def cmd_repo_snapshot(args) -> int:
    """Tar the filtered repo snapshot to /tmp/e2e-repo.tgz.

    Uses the changed-files.txt written by the pr-context stage: every PR-touched
    file is included regardless of size; the remainder is size-limited.
    """
    changed_file = Path("/tmp/pr-context/changed-files.txt")
    if changed_file.exists():
        changed = frozenset(
            line.strip()
            for line in changed_file.read_text().splitlines()
            if line.strip())
    else:
        log("no /tmp/pr-context/changed-files.txt; snapshot includes small files only")
        changed = frozenset()
    data = repo_file_list(changed, args.max_size)
    with open("/tmp/e2e-repo-files.zlist", "wb") as out:
        out.write(data)
    log("snapshot file list: %d bytes" % len(data))
    proc = subprocess.run(["tar", "--null", "-T", "/tmp/e2e-repo-files.zlist",
                           "-czf", "/tmp/e2e-repo.tgz"], check=False)
    if proc.returncode != 0 or not Path("/tmp/e2e-repo.tgz").exists():
        log("tar of the repo snapshot failed")
        return 1
    log("repository snapshot staged at /tmp/e2e-repo.tgz")
    print("repo_tgz_ok")
    return 0
def cmd_release(args) -> int:
    """Best-effort cleanup: release the claim (404 tolerated) and stop the
    background processes this script started (MCP proxy, frame capture,
    otel shutdown)."""
    try:
        return cmd_claim_delete(SimpleNamespace(pool=args.pool, name=args.name))
    finally:
        for pid_file in ("/tmp/mcp-proxy.pid", "/tmp/e2e-frame-capture.pid",
                         "/tmp/e2e-otel-shutdown.pid"):
            path = Path(pid_file)
            if not path.exists():
                continue
            try:
                pid = int(path.read_text().strip() or 0)
                if pid > 0:
                    os.kill(pid, signal.SIGTERM)
                    log(f"stopped background process {pid} ({pid_file})")
            except (ValueError, ProcessLookupError, OSError):
                pass
            path.unlink(missing_ok=True)






def cmd_pr_context(args) -> int:
    """Gather PR details + diff context files; writes GITHUB_OUTPUT keys."""
    require_gh(args.token)
    pr = json.loads(gh_json(args.pr_number, args.token,
                            "title,body,author,headRefName,headRefOid,headRepository,headRepositoryOwner,baseRefName,state,isDraft,mergeable,reviewDecision,labels,milestone"))
    out_dir = Path(env("E2E_CONTEXT_DIR", "/tmp/pr-context"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pr-details.json").write_text(json.dumps(pr, indent=2))
    (out_dir / "pr-diff.patch").write_text(
        subprocess.check_output(
            ["gh", "pr", "diff", args.pr_number, "--repo", repository_name()],
            env={**os.environ, "GH_TOKEN": args.token}, text=True))
    (out_dir / "changed-files.txt").write_text(
        gh_json(args.pr_number, args.token, "files", ".files[] | .path"))
    (out_dir / "file-stats.txt").write_text(
        gh_json(args.pr_number, args.token, "files",
                '.files[] | .path + " +(" + (.additions|tostring) + " -(" + (.deletions|tostring) + ")"'))
    (out_dir / "commits.txt").write_text(
        gh_json(args.pr_number, args.token, "commits",
                '.commits[] | "[" + .oid[:7] + "] " + .messageHeadline'))
    outputs = {
        "pr_number": args.pr_number,
        "pr_title": pr.get("title", ""),
        "head_ref": pr.get("headRefName", ""),
        "head_sha": pr.get("headRefOid", ""),
        "base_ref": pr.get("baseRefName", ""),
        "pr_author": (pr.get("author") or {}).get("login", ""),
    }
    for key in ("pr_number", "pr_title", "head_ref", "head_sha", "base_ref", "pr_author"):
        append_output(key, outputs[key])
    return 0


def cmd_comment(args) -> int:
    """Upsert the E2E result comment on the PR (marker-based, idempotent)."""
    require_gh(args.token)
    body = Path(args.body_file).read_text() if args.body_file else sys.stdin.read()
    env = dict(os.environ)
    env["GH_TOKEN"] = args.token
    proc = subprocess.run(
        ["gh", "api", f"repos/{repository_name()}/issues/{args.pr_number}/comments"],
        env=env, capture_output=True, text=True, check=True)
    comments = json.loads(proc.stdout or "[]")
    marker = "<!-- e2e-test-agent -->"
    existing = next((c for c in comments
                     if c.get("user", {}).get("type") == "Bot"
                     and marker in (c.get("body") or "")), None)
    if existing:
        subprocess.run(["gh", "api", "-X", "PATCH",
                        f"repos/{repository_name()}/issues/comments/{existing['id']}",
                        "-f", f"body={body}"], env=env, check=True)
        log(f"updated comment {existing['id']}")
    else:
        subprocess.run(["gh", "api", "-X", "POST",
                        f"repos/{repository_name()}/issues/{args.pr_number}/comments",
                        "-f", f"body={body}"], env=env, check=True)
        log("created new PR comment")
    return 0


def cmd_conclusion(args) -> int:
    """Exit non-zero unless the E2E result file says the run passed."""
    if not Path("e2e-result.json").exists():
        log("no e2e-result.json; treating the run as a failure")
        return 1
    status = json.loads(Path("e2e-result.json").read_text()).get("status", "error")
    print(f"E2E status: {status}")
    return 0 if status == "pass" else 1


def cmd_recording_start(args) -> int:
    """Start screenshot capture, falling back to server/ffmpeg recording."""
    frame_dir = Path(args.frame_dir)
    if frame_dir.exists():
        shutil.rmtree(frame_dir)
    frame_dir.mkdir(parents=True, exist_ok=True)
    pid_file = Path("/tmp/e2e-frame-capture.pid")
    log_file = Path("/tmp/e2e-frame-capture.log")
    proc = subprocess.Popen(
        [sys.executable, __file__, "capture-frames",
         "--pool", args.pool, "--sandbox", args.sandbox,
         "--output-dir", str(frame_dir), "--interval", str(args.interval)],
        stdin=subprocess.DEVNULL, stdout=open(log_file, "wb"),
        stderr=subprocess.STDOUT, start_new_session=True)
    pid_file.write_text(str(proc.pid) + "\n")
    method = "none"
    for _ in range(30):
        if any(p.stat().st_size > 0 for p in frame_dir.glob("frame-*.png")):
            method = "screenshots"
            break
        time.sleep(1)
    if method == "none":
        log("screenshot capture produced no frames; falling back to guest recording")
        try:
            proc.terminate()
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
        server_args = SimpleNamespace(pool=args.pool, sandbox=args.sandbox,
                                      service=args.service,
                                      server_service=args.server_service)
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as sink:
            cmd_start_recording(server_args)
        method = sink.getvalue().strip() or "none"
    print(f"Recording method: {method}")
    append_output("method", method)
    return 0


def cmd_recording_stop(args) -> int:
    """Stop capture and materialize e2e-video.mp4; sets has_video output."""
    method = args.method
    if method == "screenshots":
        pid_file = Path("/tmp/e2e-frame-capture.pid")
        log_file = Path("/tmp/e2e-frame-capture.log")
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
            pid_file.unlink(missing_ok=True)
        if log_file.exists():
            for line in log_file.read_text().splitlines()[:50]:
                print(line, file=sys.stderr)
        frames = sorted(Path(args.frame_dir).glob("frame-*.png"))
        count = len(frames)
        print(f"Captured desktop frames: {count}")
        if count:
            subprocess.run(["ffmpeg", "-y", "-framerate", "0.5",
                            "-i", os.path.join(args.frame_dir, "frame-%06d.png"),
                            "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "e2e-video.mp4"], check=False)
    elif method in ("server", "ffmpeg"):
        stop_args = SimpleNamespace(pool=args.pool, sandbox=args.sandbox,
                                    service=args.service,
                                    server_service=args.server_service,
                                    method=method, output="e2e-video.mp4")
        cmd_stop_recording(stop_args)
    else:
        log("no recording was started; nothing to download")
    video = Path("e2e-video.mp4")
    if video.exists() and video.stat().st_size > 0:
        print(f"Video downloaded: {video.stat().st_size} bytes")
        append_output("has_video", "true")
    else:
        video.unlink(missing_ok=True)
        append_output("has_video", "false")
        print("::warning::No test video was captured")
    return 0


def cmd_proxy_start(args) -> int:
    """Start the auth-injecting MCP proxy in the background; wait for /healthz."""
    log_file = Path("/tmp/mcp-proxy.log")
    pid_file = Path("/tmp/mcp-proxy.pid")
    proc = subprocess.Popen(
        [sys.executable, __file__, "proxy",
         "--pool", args.pool, "--sandbox", args.sandbox, "--port", str(args.port)],
        stdin=subprocess.DEVNULL, stdout=open(log_file, "wb"),
        stderr=subprocess.STDOUT, start_new_session=True)
    pid_file.write_text(str(proc.pid) + "\n")
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{args.port}/healthz", timeout=2).read()
            print("MCP proxy is up")
            return 0
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(2)
    for line in log_file.read_text(errors="replace").splitlines()[-40:]:
        print(line, file=sys.stderr)
    print("::error::MCP proxy did not become healthy")
    return 1


def cmd_publish_media(args) -> int:
    """Upload the recording (and preview gif) to S3; write URL files for the
    result comment. Preserves the publish step's public-domain semantics:
    public media URLs avoid the region when the bucket is fully public."""
    bucket = args.bucket
    media_key = args.media_key or f"pr{args.pr_number}/run{os.environ.get('GITHUB_RUN_ID', '0')}"
    edited = Path("e2e-video-edited.mp4")
    raw = Path("e2e-video.mp4")
    if not edited.exists() or not edited.stat().st_size > 0:
        video = "e2e-video.mp4"
    else:
        video = "e2e-video-edited.mp4"
    if not Path(video).exists():
        print("::warning::no video to publish")
        return 0

    def aws(*a):
        subprocess.run(["aws", *a], check=True)

    def upload(local, remote, ctype):
        cmd = ["s3", "cp", local, f"s3://{bucket}/{remote}", "--content-type", ctype]
        if not args.public_domain:
            cmd += ["--content-disposition", "inline"]
        aws(*cmd)

    preview_ok = subprocess.run(
        ["ffmpeg", "-y", "-i", video,
         "-vf", "fps=2,scale=720:-1:flags=lanczos,split[s0][s1];"
                "[s0]palettegen=max_colors=64[p];"
                "[s1][p]paletteuse=dither=bayer:bayer_scale=3",
         "-loop", "0", PREVIEW_GIF],
        capture_output=True).returncode == 0
    region = os.environ.get("AWS_REGION", "us-west-2")
    url_domain = (f"{bucket}.s3.amazonaws.com" if args.public_domain
                  else f"{bucket}.s3.{region}.amazonaws.com")
    upload(video, f"{media_key}/e2e-video.mp4", "video/mp4")
    if preview_ok:
        upload(PREVIEW_GIF, f"{media_key}/e2e-video-preview.gif", "image/gif")
    if video == "e2e-video-edited.mp4" and raw.exists():
        upload("e2e-video.mp4", f"{media_key}/e2e-video-full.mp4", "video/mp4")
        Path(FULL_URL_FILE).write_text(
            f"https://{url_domain}/{media_key}/e2e-video-full.mp4\n")
    Path(VIDEO_URL_FILE).write_text(
        f"https://{url_domain}/{media_key}/e2e-video.mp4\n")
    if preview_ok:
        Path(PREVIEW_URL_FILE).write_text(
        f"https://{url_domain}/{media_key}/e2e-video-preview.gif\n")
    log(f"published media under s3://{bucket}/{media_key}/")
    return 0


# ── CLI dispatch ─────────────────────────────────────────────────────────────

def add_sandbox_args(parser, with_sandbox=True):
    parser.add_argument("--pool", required=True, help="pool name (== namespace)")
    if with_sandbox:
        parser.add_argument("--sandbox", required=True, help="bound sandbox name")
        parser.add_argument("--service", default="mcp",
                            help="cua-driver MCP service name from the template")


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="cmd", required=True)

    for phase, phase_func in (
        ("plan", run_planning),
        ("execute", run_execution),
        ("edit-video", run_video_edit),
    ):
        p = sub.add_parser(phase)
        _add_agent_options(p)
        p.set_defaults(func=phase_func)

    def legacy(name, func, setup):
        p = sub.add_parser(name)
        setup(p)
        p.set_defaults(func=func)
        return p

    legacy("claim-create", cmd_claim_create,
           lambda p: (add_sandbox_args(p, with_sandbox=False),
                      p.add_argument("--name", required=True),
                      p.add_argument("--template", default="",
                                     help="defaults to <pool>-template"),
                      p.add_argument("--lease-minutes", type=int, default=75,
                                     help="claim shutdownTime horizon; must outlive the job"),
                      p.add_argument("--bind-deadline", type=int, default=900)))
    legacy("claim-wait", cmd_claim_wait,
           lambda p: (add_sandbox_args(p, with_sandbox=False),
                      p.add_argument("--name", required=True),
                      p.add_argument("--timeout", type=int, default=900)))
    legacy("claim-delete", cmd_claim_delete,
           lambda p: (add_sandbox_args(p, with_sandbox=False),
                      p.add_argument("--name", required=True)))
    legacy("exec", cmd_exec,
           lambda p: (add_sandbox_args(p),
                      p.add_argument("--wait-ready", type=int, default=0,
                                     help="seconds to retry while the guest MCP is still booting"),
                      p.add_argument("--timeout", type=int, default=600),
                      p.add_argument("command", nargs="?", default="",
                                     help="shell command; reads stdin when omitted")))
    legacy("upload", cmd_upload,
           lambda p: (add_sandbox_args(p),
                      p.add_argument("source", help="local file to upload"),
                      p.add_argument("destination", help="absolute destination path inside the sandbox")))
    legacy("proxy", cmd_proxy,
           lambda p: (add_sandbox_args(p),
                      p.add_argument("--port", type=int, default=3333)))
    legacy("capture-frames", cmd_capture_frames,
           lambda p: (add_sandbox_args(p),
                      p.add_argument("--output-dir", required=True),
                      p.add_argument("--interval", type=float, default=2.0)))
    for name, func in (("start-recording", cmd_start_recording),
                       ("stop-recording", cmd_stop_recording)):
        def setup(p, _f=func, _n=name):
            add_sandbox_args(p)
            p.add_argument("--server-service", default="",
                           help="optional custom guest server service suffix; defaults to the base sandbox service")
            if _n == "stop-recording":
                p.add_argument("--method", required=True, choices=["server", "ffmpeg", "none"])
                p.add_argument("--output", required=True)
        legacy(name, func, setup)

    p = sub.add_parser("provision",
                       help="claim a sandbox (+ optional repo snapshot file list)")
    add_sandbox_args(p)
    p.add_argument("--name", required=True)
    p.add_argument("--lease-minutes", type=int, default=75)
    p.add_argument("--bind-deadline", type=int, default=900)
    p.add_argument("--token", required=True, help="GitHub token for gh CLI")
    p.add_argument("--pr-number", required=True)
    p.add_argument("--repo-dir", default=".", help="checkout root to snapshot")
    p.add_argument("--snapshot", action="store_true", default=False,
                   help="also stage the repo snapshot + file list")
    p.add_argument("--max-size", type=int, default=1024 * 1024)
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("release", help="release the claim + stop background procs")
    add_sandbox_args(p, with_sandbox=False)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("pr-context", help="gather PR details + diff context files")
    p.add_argument("--token", default=env("GH_TOKEN"))
    p.add_argument("--pr-number", required=True)
    p.set_defaults(func=cmd_pr_context)

    p = sub.add_parser("recording-start",
                       help="start screenshots capture (server/ffmpeg fallback)")
    add_sandbox_args(p)
    p.add_argument("--frame-dir", default="/tmp/e2e-frames")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--server-service", default="server")
    p.set_defaults(func=cmd_recording_start)

    p = sub.add_parser("recording-stop",
                       help="stop capture, materialize e2e-video.mp4")
    add_sandbox_args(p)
    p.add_argument("--method", required=True,
                   choices=["screenshots", "server", "ffmpeg", "none"])
    p.add_argument("--frame-dir", default="/tmp/e2e-frames")
    p.add_argument("--server-service", default="server")
    p.set_defaults(func=cmd_recording_stop)

    p = sub.add_parser("proxy-start",
                       help="start the MCP auth proxy in the background")
    add_sandbox_args(p)
    p.add_argument("--port", type=int, default=3333)
    p.set_defaults(func=cmd_proxy_start)

    p = sub.add_parser("publish-media",
                       help="upload recording + preview to the media bucket")
    p.add_argument("--pr-number", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--media-key", default="", help="defaults to pr<PR>/run<RUN_ID>")
    p.add_argument("--public-domain", action="store_true", default=False,
                   help="use the region-less domain for fully-public buckets")
    p.set_defaults(func=cmd_publish_media)

    p = sub.add_parser("comment", help="upsert the E2E result comment on the PR")
    p.add_argument("--token", default=env("GH_TOKEN"))
    p.add_argument("--pr-number", required=True)
    p.add_argument("--body-file", default="",
                   help="comment body file; reads stdin when omitted")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("conclusion",
                       help="exit non-zero unless e2e-result.json says pass")
    p.set_defaults(func=cmd_conclusion)

    p = sub.add_parser("repo-snapshot",
                       help="tar the filtered repo snapshot to /tmp/e2e-repo.tgz")
    p.add_argument("--max-size", type=int, default=1024 * 1024)
    p.set_defaults(func=cmd_repo_snapshot)

    return top

def _add_agent_options(result: argparse.ArgumentParser) -> None:
    result.add_argument(
        "--planning-prompt",
        type=Path,
        default=Path(env("E2E_PLANNING_PROMPT", str(Path(__file__).resolve().parent / "prompts/planning.md"))),
    )
    result.add_argument(
        "--execution-prompt",
        type=Path,
        default=Path(env("E2E_EXECUTION_PROMPT", str(Path(__file__).resolve().parent / "prompts/execution.md"))),
    )
    result.add_argument("--repository", default=env("E2E_REPOSITORY", env("GITHUB_REPOSITORY")))
    result.add_argument("--pr-number", default=env("PR_NUMBER"))
    result.add_argument("--pr-title", default=env("PR_TITLE"))
    result.add_argument("--head-ref", default=env("HEAD_REF"))
    result.add_argument("--base-ref", default=env("BASE_REF"))
    result.add_argument("--head-sha", default=env("HEAD_SHA"))
    result.add_argument("--pr-author", default=env("PR_AUTHOR"))
    result.add_argument(
        "--sandbox-repo-dir",
        default=env("SANDBOX_REPO_DIR", "/tmp/e2e-repo"),
    )
    result.add_argument("--repo-ready", default=env("REPO_READY", "false"))
    result.add_argument(
        "--context-dir",
        type=Path,
        default=Path(env("E2E_CONTEXT_DIR", "/tmp/pr-context")),
    )
    result.add_argument(
        "--planning-dir",
        type=Path,
        default=Path(env("E2E_PLANNING_DIR", "/tmp/e2e-planning")),
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=Path(env("GITHUB_WORKSPACE", os.getcwd())),
    )
    result.add_argument(
        "--mcp-base-url",
        default=env(
            "E2E_MCP_BASE_URL",
            f"http://127.0.0.1:{env('MCP_PROXY_PORT', '3333')}",
        ),
    )
    result.add_argument(
        "--video-editing-prompt",
        type=Path,
        default=Path(env("E2E_VIDEO_EDITING_PROMPT", str(Path(__file__).resolve().parent / "prompts/video-editing.md"))),
    )
    result.add_argument(
        "--video-editor",
        type=Path,
        default=Path(env("E2E_VIDEO_EDITOR", str(Path(__file__).resolve().parent / "edit_video.py"))),
    )
    result.add_argument(
        "--video-edit-dir",
        type=Path,
        default=Path(env("E2E_VIDEO_EDIT_DIR", "/tmp/e2e-video-edit")),
    )
    result.add_argument(
        "--video-summary",
        type=Path,
        default=Path(
            env("E2E_VIDEO_SUMMARY", "/tmp/e2e-video-edit-summary.txt")
        ),
    )




_PHASE_SUBCOMMANDS = ("plan", "execute", "edit-video")


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd in _PHASE_SUBCOMMANDS:
        bundled_files = (
            args.planning_prompt,
            args.execution_prompt,
            args.video_editing_prompt,
            args.video_editor,
        )
        if not all(path.is_file() for path in bundled_files):
            github_error("Bundled E2E agent resources are unavailable")
            return 1
    try:
        if args.cmd in _PHASE_SUBCOMMANDS:
            args.repository = repository_name(args.repository)
        return args.func(args)
    except ValueError as error:
        github_error(str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
