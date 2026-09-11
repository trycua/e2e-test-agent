"""Opt-in release gate against the installed native Fleet SDK, without a cluster.

E2E_FLEET_SDK_CONTRACT=1 python3 -m unittest discover -s tests \
    -p test_claim_sdk_contract.py -v

Run with a wheel containing SDK-owned capacity retries before promoting the
new operator/action combination. Public wheels without that change must fail
this gate; it is intentionally not replaced by mocked SDK behavior.
"""

import contextlib
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest import mock

from test_agent import load_agent_module


@unittest.skipUnless(os.environ.get('E2E_FLEET_SDK_CONTRACT') == '1', 'requires retry-capable native Fleet SDK')
class NativeClaimSdkContractTests(unittest.TestCase):
    def run_server(self, initially_bound, missing_template=False):
        state = {'bound': initially_bound, 'patches': [], 'gets': 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body):
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def claim(self):
                return {
                    'apiVersion': 'osgym.cua.ai/v1alpha1', 'kind': 'OSGymSandboxClaim',
                    'metadata': {'namespace': 'test-pool', 'name': 'test-claim', 'resourceVersion': '41'},
                    'spec': {'sandboxTemplateRef': {'name': 'actual-template'}},
                    'status': ({'phase': 'Bound', 'sandbox': {'name': 'sandbox-42'}} if state['bound'] else {
                        'phase': 'Failed', 'conditions': [
                            {'type': 'Ready', 'status': 'False', 'reason': 'NoAvailableSandbox'},
                            {'type': 'BindAttempt', 'status': 'True', 'message': 'initial'},
                        ],
                    }),
                }

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path != '/token':
                    self.send_error(404)
                    return
                self.respond({'access_token': 'test-token', 'token_type': 'Bearer', 'expires_in': 3600})

            def do_GET(self):
                if self.path.endswith('/osgymsandboxclaims/test-claim'):
                    state['gets'] += 1
                    self.respond(self.claim())
                elif self.path.endswith('/osgymsandboxtemplates/actual-template'):
                    if missing_template:
                        self.send_error(404)
                        return
                    self.respond({
                        'apiVersion': 'osgym.cua.ai/v1alpha1', 'kind': 'OSGymSandboxTemplate',
                        'metadata': {'namespace': 'test-pool', 'name': 'actual-template'},
                        'spec': {'vmTemplate': {'containerDiskImage': 'test-image', 'services': []}},
                    })
                else:
                    self.send_error(404)

            def do_PATCH(self):
                if not self.path.endswith('/osgymsandboxclaims/test-claim'):
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['patches'].append(body)
                state['bound'] = True
                self.respond(self.claim())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        agent = load_agent_module()
        endpoint = f'http://127.0.0.1:{server.server_port}'
        try:
            with mock.patch.dict(os.environ, {
                'CUA_BASE_URL': endpoint, 'CUA_TOKEN_URL': endpoint + '/token',
                'CUA_CLIENT_ID': 'client', 'CUA_CLIENT_SECRET': 'secret',
            }), contextlib.redirect_stdout(io.StringIO()) as output:
                result = agent.cmd_claim_wait(SimpleNamespace(pool='test-pool', name='test-claim', timeout=12))
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), 'sandbox-42\n')
            return state
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()

    def test_bound_claim_uses_its_actual_template(self):
        state = self.run_server(initially_bound=True)
        self.assertEqual(state['patches'], [])

    def test_capacity_failure_is_retried_inside_native_sdk(self):
        state = self.run_server(initially_bound=False)
        self.assertEqual(state['patches'], [{'metadata': {
            'resourceVersion': '41', 'annotations': {'osgym.cua.ai/bind-attempt': '41'},
        }}])
        self.assertGreaterEqual(state['gets'], 2)

    def test_missing_bound_template_surfaces_sdk_error(self):
        from fleet_sdk import SdkError
        with self.assertRaises(SdkError.Status) as error:
            self.run_server(initially_bound=True, missing_template=True)
        self.assertEqual(error.exception.status, 404)
