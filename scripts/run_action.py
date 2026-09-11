#!/usr/bin/env python3

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tarfile
import uuid


class Config:
    def __init__(self, environment):
        required = ('E2E_REPOSITORY', 'PR_NUMBER', 'HEAD_SHA', 'E2E_REPO_DIR',
                    'E2E_WORK_DIR', 'E2E_POOL', 'GH_TOKEN', 'CUA_CLIENT_ID',
                    'CUA_CLIENT_SECRET', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL')
        for name in required:
            if not environment.get(name, '').strip():
                raise ValueError(f'Missing required configuration: {name}')
        self.repository = environment['E2E_REPOSITORY']
        self.pr_number = environment['PR_NUMBER']
        self.head_sha = environment['HEAD_SHA'].lower()
        self.pool = environment['E2E_POOL']
        patterns = ((self.repository, r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*'),
                    (self.pr_number, r'[1-9][0-9]*'), (self.head_sha, r'[0-9a-f]{40}'),
                    (self.pool, r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'))
        if any(not re.fullmatch(pattern, value) for value, pattern in patterns):
            raise ValueError('Invalid repository, PR number, head SHA, or pool')
        self.checkout = Path(environment['E2E_REPO_DIR']).resolve()
        self.work = Path(environment['E2E_WORK_DIR']).resolve()
        if not self.checkout.is_dir():
            raise ValueError('E2E_REPO_DIR must be an existing checkout')
        if self.work.is_relative_to(self.checkout) or self.checkout.is_relative_to(self.work):
            raise ValueError('Work directory must be separate from the target checkout')
        edit_video = environment.get('E2E_EDIT_VIDEO', 'false')
        if edit_video not in ('true', 'false'):
            raise ValueError('E2E_EDIT_VIDEO must be true or false')
        self.edit_video = edit_video == 'true'
        self.claim = 'e2e-' + uuid.uuid4().hex


def parse_outputs(text):
    outputs = {}
    lines = iter(text.splitlines())
    for line in lines:
        if '<<' in line:
            name, delimiter = line.split('<<', 1)
            value = []
            for part in lines:
                if part == delimiter:
                    break
                value.append(part)
            outputs[name] = '\n'.join(value)
        elif '=' in line:
            name, value = line.split('=', 1)
            outputs[name] = value
    return outputs


def sanitize_diagnostic(text, environment):
    if isinstance(text, bytes):
        text = text.decode(errors='replace')
    text = text or ''
    for name in ('GH_TOKEN', 'ANTHROPIC_API_KEY', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET'):
        if environment.get(name):
            text = text.replace(environment[name], '[REDACTED]')
    text = re.sub(r'(?im)(authorization\s*[:=]\s*).*$', r'\1[REDACTED]', text)
    text = re.sub(
        r'''(?i)(["']?(?:access_token|refresh_token|api[_-]?key|client_secret|password)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        r'\1[REDACTED]', text,
    )
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = ''.join(character for character in text if character in '\n\t' or ord(character) >= 32)
    return text[-4000:]


def run_process(command, *, cwd, env, timeout, input_text=None):
    process = subprocess.Popen(
        command, cwd=cwd, env=env, start_new_session=True, text=True,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=5)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if isinstance(error, subprocess.TimeoutExpired):
            error.output = stdout
            error.stderr = stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class Runner:
    def __init__(self, config, environment):
        self.config = config
        self.environment = dict(environment)
        self.results = config.work / 'results'
        self.context = config.work / 'context'
        self.logs = config.work / 'logs'
        self.home = config.work / 'home'
        for path in (self.results, self.context, self.logs, self.home):
            path.mkdir(parents=True, exist_ok=False)
        self.metadata = {}
        self.cleanup_errors = []

    def command_env(self, phase):
        allowed = ('PATH', 'LANG', 'SSL_CERT_FILE', 'NIX_SSL_CERT_FILE', 'XDG_DATA_DIRS')
        environment = {key: self.environment[key] for key in allowed if key in self.environment}
        environment.update({
            'HOME': str(self.home), 'GITHUB_WORKSPACE': str(self.results),
            'E2E_REPOSITORY': self.config.repository, 'PR_NUMBER': self.config.pr_number,
            'HEAD_SHA': self.config.head_sha, 'E2E_CONTEXT_DIR': str(self.context),
            'E2E_PLANNING_DIR': str(self.config.work / 'planning'),
            'E2E_VIDEO_EDIT_DIR': str(self.config.work / 'editing'),
            'E2E_VIDEO_SUMMARY': str(self.config.work / 'video-summary.txt'),
            'CLAUDE_OUTPUT_LOG': str(self.logs / 'agent.jsonl'),
            'SANDBOX_REPO_DIR': '/tmp/e2e-repo', 'REPO_READY': 'true',
            'MCP_PROXY_PORT': '3333',
        })
        environment.update(self.metadata)
        if phase in ('plan', 'execute', 'edit-video'):
            names = ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL',
                     'E2E_PLANNING_PROMPT', 'E2E_EXECUTION_PROMPT', 'E2E_VIDEO_EDITING_PROMPT')
        elif phase == 'pr-context':
            names = ('GH_TOKEN',)
        else:
            names = ('CUA_CLIENT_ID', 'CUA_CLIENT_SECRET', 'CUA_TOKEN_URL', 'CUA_BASE_URL')
        for name in names:
            if self.environment.get(name):
                environment[name] = self.environment[name]
        model_override = {'plan': 'E2E_PLANNING_MODEL', 'edit-video': 'E2E_VIDEO_EDITING_MODEL'}.get(phase)
        if model_override and self.environment.get(model_override):
            environment['ANTHROPIC_MODEL'] = self.environment[model_override]
        return environment

    def call(self, phase, *arguments, input_text=None):
        print(f'E2E phase: {phase}', flush=True)
        output = self.logs / (phase + '-' + uuid.uuid4().hex + '.output')
        environment = self.command_env(phase)
        environment['GITHUB_OUTPUT'] = str(output)
        timeout = {'plan': 1500, 'execute': 2400, 'edit-video': 900,
                   'claim-wait': 960, 'exec': 960, 'release': 90}.get(phase, 600)
        try:
            completed = run_process(
                ['e2e-test-agent', phase, *map(str, arguments)], cwd=self.results,
                env=environment, input_text=input_text, timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            diagnostic = sanitize_diagnostic(error.stderr, self.environment)
            (self.logs / f'{phase}.log').write_text(
                str(error.output or '') + str(error.stderr or '')
            )
            raise RuntimeError(f'{phase} timed out after {timeout}s\n{diagnostic}') from error
        (self.logs / f'{phase}.log').write_text(completed.stdout + completed.stderr)
        if completed.returncode:
            diagnostic = sanitize_diagnostic(completed.stderr, self.environment)
            raise RuntimeError(f'{phase} failed (exit {completed.returncode})\n{diagnostic}')
        outputs = parse_outputs(output.read_text()) if output.exists() else {}
        return completed.stdout.strip(), outputs

    def verify_checkout(self):
        actual = subprocess.check_output(
            ['git', '-c', 'core.fsmonitor=false', '-C', str(self.config.checkout),
             'rev-parse', 'HEAD'], text=True,
        ).strip()
        if actual != self.config.head_sha:
            raise ValueError('Checkout HEAD does not match the requested PR head')

    def check_pr(self, details):
        if details.get('headRefOid') != self.config.head_sha:
            raise ValueError('PR head changed; rerun against the new head')
        owner = (details.get('headRepositoryOwner') or {}).get('login', '')
        name = (details.get('headRepository') or {}).get('name', '')
        if f'{owner}/{name}'.lower() != self.config.repository.lower():
            raise ValueError('Only same-repository PRs are supported')
        self.metadata = {
            'PR_TITLE': details.get('title', ''), 'HEAD_REF': details.get('headRefName', ''),
            'BASE_REF': details.get('baseRefName', ''),
            'PR_AUTHOR': (details.get('author') or {}).get('login', ''),
        }

    def snapshot(self):
        tracked = subprocess.check_output(
            ['git', '-c', 'core.fsmonitor=false', '-C', str(self.config.checkout),
             'ls-files', '-z'],
        ).split(b'\0')
        destination = self.config.work / 'snapshot.tar.gz'
        total_size = 0
        with tarfile.open(destination, 'w:gz') as archive:
            for raw in tracked:
                if not raw:
                    continue
                relative = Path(os.fsdecode(raw))
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe tracked path')
                if any(part in ('.git', '.aws', '.ssh', '.gnupg', '.kube') for part in relative.parts):
                    continue
                if any(part == '.env' or (part.startswith('.env.') and part not in ('.env.example', '.env.sample'))
                       for part in relative.parts):
                    continue
                if relative.name in ('.npmrc', '.pypirc', '.netrc', 'credentials.json', 'id_rsa', 'id_ed25519'):
                    continue
                if relative.suffix.lower() in ('.pem', '.key', '.p12', '.pfx'):
                    continue
                source = self.config.checkout / relative
                if any(parent.is_symlink() for parent in (source, *source.parents)):
                    continue
                try:
                    metadata = source.stat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                total_size += metadata.st_size
                if total_size > 512 * 1024 * 1024:
                    raise ValueError('Repository snapshot exceeds the 512 MiB limit')
                archive.add(source, arcname=relative.as_posix(), recursive=False)
        return destination

    def run(self):
        status = 'error'
        claim_attempted = False
        sandbox = None
        recording_attempted = False
        method = 'none'
        try:
            self.verify_checkout()
            self.call('pr-context', '--pr-number', self.config.pr_number)
            self.check_pr(json.loads((self.context / 'pr-details.json').read_text()))
            self.call('plan')
            snapshot = self.snapshot()
            claim_attempted = True
            self.call('claim-create', '--pool', self.config.pool, '--name', self.config.claim,
                      '--lease-minutes', '120', '--bind-deadline', '900')
            sandbox, _ = self.call('claim-wait', '--pool', self.config.pool,
                                   '--name', self.config.claim, '--timeout', '900')
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?', sandbox):
                raise ValueError('Invalid bound sandbox name')
            target = ('--pool', self.config.pool, '--sandbox', sandbox)
            self.call('upload', *target, snapshot, '/tmp/e2e-repo.tgz')
            self.call('exec', *target, '--wait-ready', '300', '--timeout', '900', input_text=(
                'set -eu\nmkdir -p /tmp/e2e-repo\n'
                'tar -xzf /tmp/e2e-repo.tgz -C /tmp/e2e-repo --no-same-owner\n'
                'rm -f /tmp/e2e-repo.tgz\n'
                'cd /tmp/e2e-repo\n'
                'git init -q\ngit config user.name "E2E Test Agent"\n'
                'git config user.email "e2e@example.invalid"\n'
                'git add .\ngit -c core.hooksPath=/dev/null commit -qm "PR snapshot"\n'
            ))
            recording_attempted = True
            try:
                _, output = self.call('recording-start', *target,
                                      '--frame-dir', self.config.work / 'frames')
                method = output.get('method', 'none')
            except Exception:
                print('Recording unavailable; continuing the test.', flush=True)
            self.call('proxy-start', *target, '--port', '3333')
            self.call('execute')
            result = json.loads((self.results / 'e2e-result.json').read_text())
            if (not isinstance(result, dict) or result.get('status') not in ('pass', 'fail')
                    or not isinstance(result.get('summary'), str) or not result['summary'].strip()
                    or not (self.results / 'e2e-report.md').is_file()):
                raise ValueError('Execution did not produce a valid result and report')
            status = result['status']
        except (Exception, KeyboardInterrupt) as error:
            (self.results / 'runner-error.txt').write_text(f'{type(error).__name__}: {error}\n')
            print('E2E run could not complete; see runner-error.txt.', flush=True)
        finally:
            if recording_attempted and sandbox:
                try:
                    self.call('recording-stop', '--pool', self.config.pool, '--sandbox', sandbox,
                              '--method', method, '--frame-dir', self.config.work / 'frames')
                    if self.config.edit_video and (self.results / 'e2e-video.mp4').is_file():
                        self.call('edit-video')
                except Exception:
                    print('Recording finalization unavailable; preserving other artifacts.', flush=True)
            if claim_attempted:
                try:
                    self.call('release', '--pool', self.config.pool, '--name', self.config.claim)
                except Exception as error:
                    self.cleanup_errors.append(f'Claim release failed: {error}')
                    status = 'error'
            (self.results / 'runner-status.json').write_text(json.dumps({
                'status': status, 'claim': self.config.claim, 'pool': self.config.pool,
                'cleanup_errors': self.cleanup_errors,
            }, indent=2) + '\n')
        return status


def main():
    runner = None
    try:
        config = Config(os.environ)
        runner = Runner(config, os.environ)
        status = runner.run()
    except (ValueError, OSError) as error:
        print(f'E2E configuration error: {error}', file=sys.stderr)
        status = 'error'
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            stream.write(f'status={status}\n')
            if runner:
                stream.write(f'results-dir={runner.results}\n')
    print(f'E2E result: {status}', flush=True)
    return 0 if output else (0 if status == 'pass' else 1)


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
