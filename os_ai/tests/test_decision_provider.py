# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import inspect
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import litellm

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.os_ai.models import ai_provider as ai_provider_module

QUESTIONS = {'value': {'type': 'noul', 'instructions': 'Is `name` a company?'}}
STATE = {'name': 'ACME Corp'}


def ok_body(answer=None, model='jev-1.13.0'):
    return {
        'model': model,
        'answers': {'value': answer or {'type': 'noul', 'noul': 0.97}},
        'usage': {'input_tokens': 1000, 'output_tokens': 20},
    }


class _SystemOneHandler(BaseHTTPRequestHandler):
    """Local System One endpoint: records each request, replies with the next scripted response."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        server.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
        status, payload, headers = server.responses.pop(0) if server.responses else (500, {'error': 'unscripted'}, {})
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@tagged('post_install', '-at_install')
class TestDecisionProvider(TransactionCase):
    """Decision calls go through litellm.systemone() (System One format, e.g. TypeSafe Jev).

    The provider points at a local endpoint, so the request litellm actually builds is checked.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = HTTPServer(('127.0.0.1', 0), _SystemOneHandler)
        cls.server.requests, cls.server.responses = [], []
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base_url = 'http://127.0.0.1:%d' % cls.server.server_address[1]
        cls.provider = cls.env['os.ai.provider'].create({
            'name': 'TypeSafe test',
            'provider_type': 'typesafe',
            'model_name': 'jev-1.13.0',
            'api_key': 'ts-test-key',
            'base_url': cls.base_url,
            'capable_text': False,
            'capable_decision': True,
            'timeout': 30,
        })

    def call(self, *responses):
        """responses: (status, body or raw bytes, headers) replied in order."""
        self.server.requests.clear()
        self.server.responses[:] = list(responses)
        with patch.object(ai_provider_module.time, 'sleep') as sleep:
            result = self.provider.call_decision(STATE, QUESTIONS)
        return result, self.server.requests, sleep

    def test_request_and_answer(self):
        with patch.object(ai_provider_module.litellm, 'systemone', wraps=litellm.systemone) as systemone:
            result, requests, _sleep = self.call((200, ok_body(), {}))
        self.assertEqual(systemone.call_args.kwargs['timeout'], 30)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['path'], '/v1/systemone')
        self.assertEqual(requests[0]['body'], {'model': 'jev-1.13.0', 'state': STATE, 'questions': QUESTIONS})
        self.assertEqual(requests[0]['authorization'], 'Bearer ts-test-key')
        self.assertTrue(result['success'])
        self.assertEqual(result['answers']['value']['noul'], 0.97)
        self.assertEqual(json.loads(result['content']), result['answers'])
        self.assertEqual(result['model'], 'jev-1.13.0')
        self.assertEqual((result['prompt_tokens'], result['completion_tokens']), (1000, 20))
        # Priced by litellm's model map (output tokens are free)
        price = litellm.model_cost['typesafe/jev-1.13.0']['input_cost_per_token']
        self.assertEqual(result['cost_status'], 'reported')
        self.assertAlmostEqual(result['cost'], 1000 * price)

    def test_unpriced_model_has_unknown_cost(self):
        self.provider.model_name = 'jev-not-in-the-price-map'
        result, _requests, _sleep = self.call((200, ok_body(model='jev-not-in-the-price-map'), {}))
        self.assertTrue(result['success'])
        self.assertEqual((result['cost'], result['cost_status']), (0.0, 'unknown'))

    def test_custom_base_url(self):
        self.provider.base_url = self.base_url + '/v1/'
        result, requests, _sleep = self.call((200, ok_body(), {}))
        self.assertTrue(result['success'])
        self.assertEqual(requests[0]['path'], '/v1/systemone')

    def test_rate_limit_is_retried_with_retry_after(self):
        result, requests, sleep = self.call((429, {'error': 'rate limited'}, {'Retry-After': '3'}), (200, ok_body(), {}))
        self.assertTrue(result['success'])
        self.assertEqual(len(requests), 2)
        sleep.assert_called_once_with(3.0)

    def test_retries_are_bounded(self):
        result, requests, sleep = self.call(*[(529, {'error': 'overloaded'}, {})] * 5)
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertEqual(len(requests), 3)  # first call + 2 retries
        for call in sleep.call_args_list:
            self.assertLessEqual(call.args[0], 10)

    def test_invalid_key_is_permanent(self):
        result, requests, _sleep = self.call((401, {'error': 'invalid API key'}, {}))
        self.assertEqual(len(requests), 1)
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'permanent')
        self.assertIn('invalid API key', result['error'])

    def test_validation_error_is_permanent(self):
        result, requests, _sleep = self.call((422, {'detail': 'questions.value.criteria: field required'}, {}))
        self.assertEqual(result['error_kind'], 'permanent')
        self.assertEqual(len(requests), 1)

    def test_server_error_is_retried(self):
        result, requests, _sleep = self.call((500, {'error': 'internal'}, {}), (200, ok_body(), {}))
        self.assertTrue(result['success'])
        self.assertEqual(len(requests), 2)

    def test_incomplete_answer_is_not_a_success(self):
        body = ok_body()
        body['answers'] = {}
        result, _requests, _sleep = self.call((200, body, {}))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        # the call was billed: tokens, cost and raw answer are kept for the log
        self.assertEqual(result['prompt_tokens'], 1000)
        self.assertEqual(result['cost_status'], 'reported')
        self.assertTrue(result['raw_response'])

    def test_retries_stop_at_the_deadline(self):
        """No retry wait beyond the caller's deadline (cron time budget)."""
        import time
        self.provider = self.provider.with_context(os_ai_deadline=time.monotonic() + 1)
        result, requests, sleep = self.call(*[(529, {'error': 'overloaded'}, {})] * 3)
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()

    def test_wrong_answer_type_is_not_a_success(self):
        answer = {'type': 'choice', 'choice': 'x', 'confidence': 0.9, 'probabilities': {'x': 0.9}}
        result, _requests, _sleep = self.call((200, ok_body(answer), {}))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')

    def test_invalid_json_is_retryable(self):
        result, requests, sleep = self.call((200, b'{not json', {}))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertEqual(len(requests), 1)  # left to the cron, no immediate retry
        sleep.assert_not_called()

    def test_connection_error_is_retryable(self):
        with socket.socket() as sock:  # a port nothing listens on
            sock.bind(('127.0.0.1', 0))
            closed_port = sock.getsockname()[1]
        self.provider.base_url = 'http://127.0.0.1:%d' % closed_port
        result, _requests, sleep = self.call()
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        sleep.assert_not_called()  # left to the cron, no immediate retry

    def test_litellm_systemone_contract(self):
        """The litellm API this module relies on (litellm >= 1.105.0)."""
        parameters = inspect.signature(litellm.systemone).parameters
        for name in ('model', 'state', 'questions', 'api_key', 'api_base', 'timeout'):
            self.assertIn(name, parameters)
