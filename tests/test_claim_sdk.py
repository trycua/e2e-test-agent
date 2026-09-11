import asyncio
import contextlib
import io
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from test_agent import load_agent_module


class ClaimTimeout(Exception):
    pass


class ClaimFailed(Exception):
    def __init__(self, phase, status):
        self.phase = phase
        self.status = status


class ClaimSdkTests(unittest.TestCase):
    def setUp(self):
        self.agent = load_agent_module()
        self.client = SimpleNamespace(wait_claim=mock.AsyncMock(return_value=SimpleNamespace(name='sandbox-42')))
        self.sdk = SimpleNamespace(
            Claim=SimpleNamespace, ClaimSpec=SimpleNamespace,
            ResourceMetadata=SimpleNamespace, SandboxTemplateRef=SimpleNamespace,
            CyclopsConfiguration=SimpleNamespace,
            CyclopsCredentials=mock.Mock(return_value='credentials'),
            CyclopsClient=SimpleNamespace(connect_with_native_http_client=mock.Mock(return_value=self.client)),
            SdkError=SimpleNamespace(ClaimTimeout=ClaimTimeout, ClaimFailed=ClaimFailed),
        )
        self.args = SimpleNamespace(pool='test-pool', name='test-claim', timeout=30)
        self.environment = {'CUA_CLIENT_ID': 'client', 'CUA_CLIENT_SECRET': 'secret'}

    def run_wait(self):
        with mock.patch.dict('sys.modules', {'fleet_sdk': self.sdk}), \
             mock.patch.dict(os.environ, self.environment, clear=True), \
             mock.patch.object(self.agent, 'api_request', side_effect=AssertionError('raw polling is forbidden')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.agent.cmd_claim_wait(self.args)
        return result, output.getvalue()

    def test_delegates_waiting_to_sdk_without_changing_claim_identity_or_stdout(self):
        result, output = self.run_wait()
        self.assertEqual((result, output), (0, 'sandbox-42\n'))
        self.client.wait_claim.assert_awaited_once()
        claim = self.client.wait_claim.call_args.args[0]
        self.assertEqual(claim.metadata.namespace, 'test-pool')
        self.assertEqual(claim.metadata.name, 'test-claim')
        config = self.sdk.CyclopsClient.connect_with_native_http_client.call_args.args[0]
        self.assertEqual(config.claim_poll_interval_ms, 5000)
        self.assertEqual(config.claim_poll_limit, 7)
        self.sdk.CyclopsCredentials.assert_called_once_with('client', 'secret')

    def test_preserves_endpoint_overrides(self):
        self.environment.update(CUA_BASE_URL='https://fleet.example/prefix/', CUA_TOKEN_URL='https://id.example/token')
        self.run_wait()
        config = self.sdk.CyclopsClient.connect_with_native_http_client.call_args.args[0]
        self.assertEqual(config.base_url, 'https://fleet.example/prefix')
        self.assertEqual(config.token_url, 'https://id.example/token')

    def test_sdk_timeout_preserves_failure_exit(self):
        self.client.wait_claim.side_effect = ClaimTimeout()
        with mock.patch.object(self.agent, 'log') as log:
            self.assertEqual(self.run_wait(), (1, ''))
        self.assertIn('not Bound after 30s', log.call_args.args[0])

    def test_permanent_failure_is_not_retried_by_action(self):
        self.client.wait_claim.side_effect = ClaimFailed('Failed', '{"reason":"TemplateRefMissing"}')
        with mock.patch.object(self.agent, 'log') as log:
            self.assertEqual(self.run_wait(), (1, ''))
        self.client.wait_claim.assert_awaited_once()
        self.assertIn('TemplateRefMissing', log.call_args.args[0])

    def test_wall_clock_timeout_cancels_sdk_wait(self):
        cancelled = []

        async def blocked(claim):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

        self.client.wait_claim.side_effect = blocked
        self.args.timeout = 0.01
        with mock.patch.object(self.agent, 'log'):
            self.assertEqual(self.run_wait(), (1, ''))
        self.assertEqual(cancelled, [True])

    def test_zero_timeout_does_not_start_a_client(self):
        self.args.timeout = 0
        with mock.patch.object(self.agent, 'log'):
            self.assertEqual(self.run_wait(), (1, ''))
        self.sdk.CyclopsClient.connect_with_native_http_client.assert_not_called()
