import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_agent import load_agent_module


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.agent = load_agent_module()

    def test_resources_work_without_nix_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = self.agent.build_parser().parse_args(['plan'])
        for path in (args.planning_prompt, args.execution_prompt,
                     args.video_editing_prompt, args.video_editor):
            self.assertTrue(path.is_file(), str(path))

    def test_resource_overrides_are_preserved(self):
        with mock.patch.dict(os.environ, {'E2E_PLANNING_PROMPT': '/custom/plan.md'}):
            args = self.agent.build_parser().parse_args(['plan'])
        self.assertEqual(args.planning_prompt, Path('/custom/plan.md'))

    def test_missing_repository_fails_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'repository'):
                self.agent.repository_name()

    def test_repository_override_takes_precedence(self):
        with mock.patch.dict(os.environ, {
            'E2E_REPOSITORY': 'other/application',
            'GITHUB_REPOSITORY': 'caller/repository',
        }, clear=True):
            self.assertEqual(self.agent.repository_name(), 'other/application')

    def test_repository_must_be_owner_and_name(self):
        for value in ('', '../repo', 'https://github.com/example/project', 'a/b/c', 'a/b\nx=y'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    self.agent.repository_name(value)

    def test_gh_calls_use_explicit_repository(self):
        with mock.patch.dict(os.environ, {'E2E_REPOSITORY': 'other/application'}, clear=True):
            with mock.patch.object(self.agent.subprocess, 'check_output', return_value='{}') as command:
                self.agent.gh_json('12', 'secret', 'title')
        self.assertEqual(command.call_args.args[0], [
            'gh', 'pr', 'view', '12', '--repo', 'other/application', '--json', 'title',
        ])

    def test_private_secret_commands_are_not_public_interface(self):
        parser = self.agent.build_parser()
        for name in ('load-cyclops-creds', 'load-litellm-creds'):
            with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([name, '--secret-id', 'private'])

    def test_github_outputs_cannot_inject_additional_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
                self.agent.append_output('title', 'hello\nmalicious=true')
            content = output.read_text().splitlines()
        self.assertTrue(content[0].startswith('title<<'))
        self.assertEqual(content[-1], content[0].split('<<')[1])
        self.assertEqual(content[1:-1], ['hello', 'malicious=true'])


    def test_release_stops_children_even_when_claim_deletion_fails(self):
        pid_file = mock.Mock()
        pid_file.exists.return_value = True
        pid_file.read_text.return_value = '1234'
        args = mock.Mock(pool='pool', name='claim')
        with mock.patch.object(self.agent, 'cmd_claim_delete', side_effect=RuntimeError('unavailable')), \
             mock.patch.object(self.agent, 'Path', return_value=pid_file), \
             mock.patch.object(self.agent.os, 'kill') as kill:
            with self.assertRaises(RuntimeError):
                self.agent.cmd_release(args)
        self.assertEqual(kill.call_count, 3)
        self.assertEqual(pid_file.unlink.call_count, 3)


if __name__ == '__main__':
    unittest.main()
