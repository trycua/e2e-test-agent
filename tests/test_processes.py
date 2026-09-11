import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_action


class ProcessTests(unittest.TestCase):
    def test_diagnostics_redact_configured_and_returned_tokens(self):
        text = ('Authentication failed for configured-secret\n'
                'Authorization: Bearer returned-secret\n'
                '{"access_token": "oauth-returned-secret"}\n')
        result = run_action.sanitize_diagnostic(text, {'ANTHROPIC_API_KEY': 'configured-secret'})
        self.assertIn('Authentication failed', result)
        for secret in ('configured-secret', 'returned-secret', 'oauth-returned-secret'):
            self.assertNotIn(secret, result)

    def test_diagnostics_are_bounded(self):
        result = run_action.sanitize_diagnostic('x' * 10000, {})
        self.assertLessEqual(len(result), 4000)

    def test_success_captures_stdout_and_stderr(self):
        result = run_action.run_process(
            [sys.executable, '-c', 'import sys; print("out"); print("err", file=sys.stderr)'],
            cwd=Path.cwd(), env=os.environ, timeout=5,
        )
        self.assertEqual(result.stdout, 'out\n')
        self.assertEqual(result.stderr, 'err\n')
        self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux process verification')
    def test_timeout_terminates_descendants_and_preserves_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child.pid'
            script = (
                'import subprocess, sys, time\n'
                'from pathlib import Path\n'
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                f'Path({str(marker)!r}).write_text(str(child.pid))\n'
                'print("diagnostic before timeout", file=sys.stderr, flush=True)\n'
                'time.sleep(60)\n'
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    run_action.run_process([sys.executable, '-c', script],
                                           cwd=Path(directory), env=os.environ, timeout=1)
                self.assertIn('diagnostic before timeout', caught.exception.stderr)
                self.assertTrue(marker.is_file())
                process_state = Path('/proc') / marker.read_text() / 'stat'
                for _ in range(40):
                    if not process_state.exists() or process_state.read_text().split()[2] == 'Z':
                        break
                    time.sleep(0.05)
                else:
                    self.fail('Descendant survived phase timeout')
            finally:
                if marker.exists():
                    try:
                        os.kill(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    unittest.main()
