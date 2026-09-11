import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_action


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.env = {
            'E2E_REPOSITORY': 'example/project', 'PR_NUMBER': '12',
            'HEAD_SHA': 'a' * 40, 'E2E_REPO_DIR': str(self.checkout),
            'E2E_WORK_DIR': str(self.root / 'work'), 'E2E_POOL': 'desktop',
            'ANTHROPIC_API_KEY': 'model-secret', 'ANTHROPIC_MODEL': 'test-model',
            'GH_TOKEN': 'github-secret', 'CUA_CLIENT_ID': 'client',
            'CUA_CLIENT_SECRET': 'cua-secret', 'PATH': os.environ['PATH'],
        }

    def runner(self):
        return run_action.Runner(run_action.Config(self.env), self.env)

    def test_system_prompt_is_only_forwarded_to_planning_and_execution(self):
        self.env['E2E_SYSTEM_PROMPT'] = '/trusted/custom-system.md'
        runner = self.runner()
        for phase in ('plan', 'execute'):
            self.assertEqual(runner.command_env(phase)['E2E_SYSTEM_PROMPT'],
                             '/trusted/custom-system.md')
        for phase in ('pr-context', 'claim-create', 'edit-video'):
            self.assertNotIn('E2E_SYSTEM_PROMPT', runner.command_env(phase))

    def test_required_configuration_fails_before_running(self):
        for key in ('E2E_REPOSITORY', 'PR_NUMBER', 'HEAD_SHA', 'E2E_POOL',
                    'GH_TOKEN', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET', 'ANTHROPIC_API_KEY',
                    'ANTHROPIC_MODEL'):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    run_action.Config({**self.env, key: ''})

    def test_malformed_configuration_is_rejected(self):
        for key, value in [('PR_NUMBER', '-1'), ('HEAD_SHA', 'main'),
                           ('E2E_POOL', 'pool/../../other'), ('E2E_EDIT_VIDEO', 'maybe')]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    run_action.Config({**self.env, key: value})

    def test_workspace_cannot_be_inside_target_checkout(self):
        with self.assertRaises(ValueError):
            run_action.Config({**self.env, 'E2E_WORK_DIR': str(self.checkout / 'results')})

    def test_agent_does_not_inherit_infrastructure_credentials(self):
        runner = self.runner()
        environment = runner.command_env('execute')
        for key in ('GH_TOKEN', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET', 'GITHUB_ENV', 'GITHUB_PATH'):
            self.assertNotIn(key, environment)
        self.assertEqual(environment['ANTHROPIC_API_KEY'], 'model-secret')
        self.assertNotEqual(environment['HOME'], os.environ.get('HOME'))

    def test_optional_per_phase_models_preserve_execution_default(self):
        self.env.update({'E2E_PLANNING_MODEL': 'planner', 'E2E_VIDEO_EDITING_MODEL': 'editor'})
        runner = self.runner()
        self.assertEqual(runner.command_env('plan')['ANTHROPIC_MODEL'], 'planner')
        self.assertEqual(runner.command_env('execute')['ANTHROPIC_MODEL'], 'test-model')
        self.assertEqual(runner.command_env('edit-video')['ANTHROPIC_MODEL'], 'editor')

    def test_explicit_sdk_effort_and_metrics_settings_reach_only_model_phases(self):
        settings = {
            'CLAUDE_CODE_EFFORT_LEVEL': 'medium', 'CLAUDE_CODE_ENABLE_TELEMETRY': '1',
            'OTEL_METRICS_EXPORTER': 'otlp', 'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/protobuf',
            'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://metrics.example',
        }
        self.env.update(settings)
        runner = self.runner()
        for phase in ('plan', 'execute', 'edit-video'):
            for name, value in settings.items():
                self.assertEqual(runner.command_env(phase).get(name), value)
        for name in settings:
            self.assertNotIn(name, runner.command_env('pr-context'))

    def test_snapshot_excludes_secrets_symlinks_and_untracked_files(self):
        subprocess.run(['git', 'init', '-q', str(self.checkout)], check=True)
        (self.checkout / 'app.txt').write_text('application')
        (self.checkout / '.env').write_text('SECRET=private')
        (self.checkout / '.env.example').write_text('SECRET=')
        (self.checkout / 'server.pem').write_text('private key')
        (self.checkout / 'link').symlink_to('/etc/passwd')
        (self.checkout / '.aws').mkdir()
        (self.checkout / '.aws/credentials').write_text('private')
        subprocess.run(['git', '-C', str(self.checkout), 'add', '.'], check=True)
        (self.checkout / 'untracked').write_text('must not leave runner')
        runner = self.runner()
        archive = runner.snapshot()
        with tarfile.open(archive) as bundle:
            names = bundle.getnames()
        self.assertEqual(sorted(names), ['.env.example', 'app.txt'])

    def simulate(self, failed_phase=None, result='pass', cleanup_fails=False):
        runner = self.runner()
        phases = []

        def command(phase, *args, **kwargs):
            phases.append(phase)
            if phase == failed_phase or (phase == 'release' and cleanup_fails):
                raise RuntimeError('phase failed')
            if phase == 'pr-context':
                runner.context.mkdir(exist_ok=True)
                (runner.context / 'pr-details.json').write_text(json.dumps({
                    'headRefOid': 'a' * 40, 'title': 'Test',
                    'headRepository': {'name': 'project'},
                    'headRepositoryOwner': {'login': 'example'},
                }))
            if phase == 'claim-wait':
                return 'sandbox-123', {}
            if phase == 'recording-start':
                return '', {'method': 'screenshots'}
            if phase == 'execute':
                (runner.results / 'e2e-result.json').write_text(json.dumps({
                    'status': result, 'summary': 'Observed result',
                }))
                (runner.results / 'e2e-report.md').write_text('Observed report')
            return '', {}

        with mock.patch.object(runner, 'call', side_effect=command), \
             mock.patch.object(runner, 'verify_checkout'), \
             mock.patch.object(runner, 'snapshot', return_value=self.root / 'snapshot.tar.gz'):
            status = runner.run()
        return runner, phases, status

    def test_success_orders_phases_and_always_releases_claim(self):
        runner, phases, status = self.simulate()
        self.assertEqual(status, 'pass')
        self.assertLess(phases.index('plan'), phases.index('claim-create'))
        self.assertLess(phases.index('upload'), phases.index('execute'))
        self.assertLess(phases.index('execute'), phases.index('recording-stop'))
        self.assertEqual(phases[-1], 'release')
        self.assertTrue((runner.results / 'e2e-result.json').is_file())

    def test_creation_failure_still_attempts_release(self):
        _, phases, status = self.simulate(failed_phase='claim-create')
        self.assertEqual(status, 'error')
        self.assertIn('release', phases)

    def test_binding_failure_still_attempts_release(self):
        _, phases, status = self.simulate(failed_phase='claim-wait')
        self.assertEqual(status, 'error')
        self.assertIn('release', phases)

    def test_planning_failure_does_not_create_claim(self):
        _, phases, status = self.simulate(failed_phase='plan')
        self.assertEqual(status, 'error')
        self.assertNotIn('claim-create', phases)

    def test_execution_failure_stops_recording_and_releases(self):
        _, phases, status = self.simulate(failed_phase='execute')
        self.assertEqual(status, 'error')
        self.assertIn('recording-stop', phases)
        self.assertIn('release', phases)

    def test_optional_recording_failure_does_not_skip_execution(self):
        _, phases, status = self.simulate(failed_phase='recording-start')
        self.assertEqual(status, 'pass')
        self.assertIn('execute', phases)
        self.assertIn('release', phases)

    def test_cleanup_failure_is_not_reported_as_success(self):
        _, _, status = self.simulate(cleanup_fails=True)
        self.assertEqual(status, 'error')

    def test_failed_journey_stays_failed(self):
        _, _, status = self.simulate(result='fail')
        self.assertEqual(status, 'fail')

    def test_invalid_result_cannot_pass(self):
        _, _, status = self.simulate(result='invented')
        self.assertEqual(status, 'error')

    def test_pr_head_must_match_expected_checkout(self):
        runner = self.runner()
        with self.assertRaisesRegex(ValueError, 'head'):
            runner.check_pr({'headRefOid': 'b' * 40})

    def test_fork_pr_is_rejected(self):
        runner = self.runner()
        with self.assertRaisesRegex(ValueError, 'same-repository'):
            runner.check_pr({'headRefOid': 'a' * 40,
                             'headRepository': {'name': 'fork'},
                             'headRepositoryOwner': {'login': 'outsider'}})

    def test_failed_phase_preserves_sanitized_stderr_not_stdout(self):
        runner = self.runner()
        completed = subprocess.CompletedProcess(
            [], 1, 'private repository content', 'API rejected model-secret',
        )
        with mock.patch.object(run_action, 'run_process', return_value=completed):
            with self.assertRaises(RuntimeError) as caught:
                runner.call('plan')
        self.assertIn('API rejected', str(caught.exception))
        self.assertNotIn('model-secret', str(caught.exception))
        self.assertNotIn('private repository content', str(caught.exception))

    def test_timeout_preserves_sanitized_diagnostic(self):
        runner = self.runner()
        error = subprocess.TimeoutExpired(['agent'], 10, stderr='gateway timeout cua-secret')
        with mock.patch.object(run_action, 'run_process', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                runner.call('claim-wait')
        self.assertIn('gateway timeout', str(caught.exception))
        self.assertNotIn('cua-secret', str(caught.exception))

    def test_multiline_outputs_are_parsed(self):
        self.assertEqual(run_action.parse_outputs('method<<abc\nscreenshots\nabc\n'),
                         {'method': 'screenshots'})


if __name__ == '__main__':
    unittest.main()
