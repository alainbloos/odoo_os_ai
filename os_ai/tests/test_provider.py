# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.os_ai.models import ai_provider as ai_provider_module


class FakeApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def fake_completion(content='{"value": "ok"}', finish_reason='stop'):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    return SimpleNamespace(choices=[choice], usage=usage, model_dump=lambda: {'fake': True})


@tagged('post_install', '-at_install')
class TestProviderCall(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env['os.ai.provider'].create({
            'name': 'Mocked provider',
            'provider_type': 'openai',
            'model_name': 'test-model',
        })

    def call(self, side_effect=None, return_value=None, response_format=None):
        with patch.object(ai_provider_module.litellm, 'completion',
                          side_effect=side_effect, return_value=return_value) as completion:
            result = self.provider.call_llm('system', 'user prompt', response_format=response_format)
        return result, completion

    def test_success(self):
        result, completion = self.call(return_value=fake_completion())
        self.assertTrue(result['success'])
        self.assertEqual(result['content'], '{"value": "ok"}')
        self.assertEqual(completion.call_args.kwargs['model'], 'openai/test-model')

    def test_rate_limit_is_retryable(self):
        result, _completion = self.call(side_effect=FakeApiError('Rate limit', status_code=429))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')

    def test_server_error_is_retryable(self):
        result, _completion = self.call(side_effect=FakeApiError('Overloaded', status_code=503))
        self.assertEqual(result['error_kind'], 'retryable')

    def test_auth_error_is_permanent(self):
        result, _completion = self.call(side_effect=FakeApiError('Invalid key', status_code=401))
        self.assertEqual(result['error_kind'], 'permanent')

    def test_bad_request_is_permanent(self):
        result, _completion = self.call(side_effect=FakeApiError('Unsupported parameter', status_code=400))
        self.assertEqual(result['error_kind'], 'permanent')

    def test_connection_error_without_status_is_retryable(self):
        result, _completion = self.call(side_effect=ConnectionError('network down'))
        self.assertEqual(result['error_kind'], 'retryable')

    def test_truncated_answer_is_not_a_success(self):
        result, _completion = self.call(return_value=fake_completion('{"value": "cut', finish_reason='length'))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertIn('length', result['error'])

    def test_content_filter_is_permanent(self):
        result, _completion = self.call(return_value=fake_completion('', finish_reason='content_filter'))
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'permanent')


@tagged('post_install', '-at_install')
class TestProviderParameters(TransactionCase):
    """Optional parameters are only sent when configured (current models reject some)."""

    SCHEMA = {
        'type': 'json_schema',
        'json_schema': {'name': 'single_response', 'strict': True, 'schema': {
            'type': 'object', 'properties': {'value': {'type': 'string'}},
            'required': ['value'], 'additionalProperties': False,
        }},
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env['os.ai.provider'].create({
            'name': 'Parameters provider',
            'provider_type': 'openai',
            'model_name': 'gpt-6-luna',
        })

    def call(self, side_effect=None, response_format=None):
        with patch.object(ai_provider_module.litellm, 'completion',
                          side_effect=side_effect or [fake_completion()]) as completion:
            result = self.provider.call_llm('system', 'user prompt', response_format=response_format)
        return result, completion

    def test_defaults_send_no_optional_parameter(self):
        _result, completion = self.call()
        kwargs = completion.call_args.kwargs
        for param in ('temperature', 'reasoning_effort', 'max_tokens'):
            self.assertNotIn(param, kwargs)
        self.assertEqual(kwargs['timeout'], 180)
        self.assertTrue(kwargs['drop_params'])

    def test_custom_temperature_zero_is_sent(self):
        self.provider.write({'temperature_mode': 'custom', 'temperature': 0.0})
        _result, completion = self.call()
        self.assertEqual(completion.call_args.kwargs['temperature'], 0.0)

    def test_configured_parameters_are_sent(self):
        self.provider.write({'reasoning_effort': 'low', 'max_output_tokens': 500, 'timeout': 30})
        _result, completion = self.call()
        kwargs = completion.call_args.kwargs
        self.assertEqual(kwargs['reasoning_effort'], 'low')
        self.assertEqual(kwargs['max_tokens'], 500)
        self.assertEqual(kwargs['timeout'], 30)

    def test_rejected_temperature_is_retried_once_without_it(self):
        self.provider.write({'temperature_mode': 'custom', 'temperature': 0.3})
        rejected = FakeApiError("Unsupported value: 'temperature' does not support 0.3 with this model. "
                                "Only the default (1) value is supported.", status_code=400)
        result, completion = self.call(side_effect=[rejected, fake_completion()])
        self.assertTrue(result['success'])
        self.assertEqual(completion.call_count, 2)
        self.assertEqual(completion.call_args_list[0].kwargs['temperature'], 0.3)
        self.assertNotIn('temperature', completion.call_args_list[1].kwargs)

    def test_other_bad_request_is_not_retried(self):
        self.provider.write({'temperature_mode': 'custom', 'temperature': 0.3})
        result, completion = self.call(side_effect=[FakeApiError('Invalid schema for response_format', 400)])
        self.assertEqual(completion.call_count, 1)
        self.assertEqual(result['error_kind'], 'permanent')

    def test_structured_output_json_schema(self):
        _result, completion = self.call(response_format=self.SCHEMA)
        self.assertEqual(completion.call_args.kwargs['response_format'], self.SCHEMA)

    def test_structured_output_json_mode_describes_the_schema(self):
        self.provider.structured_output = 'json_object'
        _result, completion = self.call(response_format=self.SCHEMA)
        kwargs = completion.call_args.kwargs
        self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
        user_message = kwargs['messages'][-1]['content']
        self.assertIn('JSON schema', user_message)
        self.assertIn('"value"', user_message)

    def test_structured_output_instructions_only(self):
        """No response_format, but the expected JSON is described."""
        self.provider.structured_output = 'none'
        _result, completion = self.call(response_format=self.SCHEMA)
        kwargs = completion.call_args.kwargs
        self.assertNotIn('response_format', kwargs)
        self.assertIn('JSON schema', kwargs['messages'][-1]['content'])

    def test_deepseek_effort_level_is_sent(self):
        """litellm only switches DeepSeek's thinking on/off; the level goes as reasoning_effort."""
        self.provider.write({'provider_type': 'deepseek', 'model_name': 'deepseek-flash', 'reasoning_effort': 'low'})
        _result, completion = self.call()
        kwargs = completion.call_args.kwargs
        self.assertEqual(kwargs['reasoning_effort'], 'low')
        self.assertEqual(kwargs['extra_body'], {'reasoning_effort': 'low'})
        self.provider.reasoning_effort = 'none'  # thinking off: litellm handles it
        _result, completion = self.call()
        self.assertNotIn('extra_body', completion.call_args.kwargs)
        self.provider.write({'provider_type': 'openai', 'model_name': 'gpt-6-luna', 'reasoning_effort': 'low'})
        _result, completion = self.call()
        self.assertNotIn('extra_body', completion.call_args.kwargs)

    def test_deadline_shortens_the_timeout(self):
        """The cron's time budget bounds every call."""
        import time
        provider = self.provider.with_context(os_ai_deadline=time.monotonic() + 20)
        self.assertLessEqual(provider._call_timeout(), 20)
        self.assertEqual(self.provider._call_timeout(), 180)
        self.provider.timeout = 0
        self.assertIsNone(self.provider._call_timeout())
        self.assertLessEqual(self.provider.with_context(os_ai_deadline=time.monotonic() + 20)._call_timeout(), 20)

    def test_no_call_after_the_deadline(self):
        """Neither the call nor the retry without temperature start after the deadline."""
        import time
        expired = self.provider.with_context(os_ai_deadline=time.monotonic() - 30)
        self.assertEqual(expired._call_timeout(), 0)
        with patch.object(ai_provider_module.litellm, 'completion', side_effect=[fake_completion()]) as completion:
            result = expired.call_llm('system', 'user prompt')
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertEqual(completion.call_count, 0)

        self.provider.write({'temperature_mode': 'custom', 'temperature': 0.3})
        provider = self.provider.with_context(os_ai_deadline=time.monotonic() + 0.2)
        rejected = FakeApiError("Unsupported value: 'temperature' does not support 0.3 with this model. "
                                "Only the default (1) value is supported.", status_code=400)

        def slow_rejection(**kwargs):
            time.sleep(0.3)  # the first call uses up the budget
            raise rejected

        with patch.object(ai_provider_module.litellm, 'completion', side_effect=slow_rejection) as completion:
            result = provider.call_llm('system', 'user prompt')
        self.assertEqual(completion.call_count, 1)
        self.assertEqual(result['error_kind'], 'retryable')

    def test_no_sdk_retry_after_the_deadline(self):
        """A call that times out at the deadline is not retried by the provider SDK
        (the OpenAI client retries twice by default); local endpoint, real litellm."""
        import json
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.server.calls += 1
                time.sleep(0.5)  # answers after the deadline
                data = json.dumps({'error': {'message': 'late'}}).encode()
                try:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up at the deadline

        server = HTTPServer(('127.0.0.1', 0), SlowHandler)
        server.calls = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.provider.write({'base_url': 'http://127.0.0.1:%d/v1' % server.server_address[1], 'api_key': 'sk-test'})
            provider = self.provider.with_context(os_ai_deadline=time.monotonic() + 0.3)
            self.assertEqual(provider._completion_kwargs('openai/gpt-6-luna', [])['max_retries'], 0)
            self.assertNotIn('max_retries', self.provider._completion_kwargs('openai/gpt-6-luna', []))
            result = provider.call_llm('system', 'user prompt')
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'], 'retryable')
        self.assertEqual(server.calls, 1)

    def test_image_download_respects_the_deadline(self):
        import time
        provider = self.provider.with_context(os_ai_deadline=time.monotonic() + 0.5)
        self.assertLessEqual(provider._download_timeout(), 0.5)
        self.assertEqual(self.provider._download_timeout(), 30)
        with self.assertRaises(ai_provider_module.TimeBudgetExceeded):
            self.provider.with_context(os_ai_deadline=time.monotonic() - 1)._download_timeout()

    def test_numeric_settings_are_validated(self):
        """Invalid settings are refused instead of being ignored."""
        from odoo.exceptions import ValidationError
        for vals in ({'timeout': -1}, {'max_output_tokens': -5}, {'temperature_mode': 'custom', 'temperature': 3}):
            with self.assertRaises(ValidationError, msg=repr(vals)):
                with self.env.cr.savepoint():
                    self.provider.write(vals)

    def test_cost_status(self):
        with patch.object(ai_provider_module.litellm, 'completion_cost', return_value=0.0012):
            result, _completion = self.call()
        self.assertEqual((result['cost'], result['cost_status']), (0.0012, 'reported'))
        with patch.object(ai_provider_module.litellm, 'completion_cost', side_effect=Exception('unmapped model')):
            result, _completion = self.call()
        self.assertEqual((result['cost'], result['cost_status']), (0.0, 'unknown'))


@tagged('post_install', '-at_install')
class TestCapabilityDetection(TransactionCase):

    def detect(self, provider_type, model_name, info):
        provider = self.env['os.ai.provider'].create({
            'name': 'Detect', 'provider_type': provider_type, 'model_name': model_name,
            'structured_output': 'json_object',
        })
        with patch.object(ai_provider_module.litellm, 'get_model_info', return_value=info):
            provider.action_detect_capabilities()
        return provider

    def test_deepseek_keeps_json_mode(self):
        """DeepSeek's API has no JSON Schema output, whatever litellm reports."""
        info = {'mode': 'chat', 'supports_vision': False, 'supports_response_schema': True}
        self.assertEqual(self.detect('deepseek', 'deepseek-flash', info).structured_output, 'json_object')
        self.assertEqual(self.detect('openai', 'gpt-6-luna', info).structured_output, 'json_schema')

    def test_decision_model(self):
        provider = self.detect('typesafe', 'jev-1.14.0', {'mode': 'evaluation'})
        self.assertTrue(provider.capable_decision)
        self.assertFalse(provider.capable_text)


@tagged('post_install', '-at_install')
class TestModelLifecycle(TransactionCase):

    def test_retired_model_has_a_suggested_replacement(self):
        provider = self.env['os.ai.provider'].create({
            'name': 'Retired', 'provider_type': 'deepseek', 'model_name': 'deepseek-chat',
        })
        self.assertEqual(provider.model_replacement, 'deepseek-flash')
        self.assertIn('deepseek-chat', provider.model_status_message)
        provider.action_apply_model_replacement()
        self.assertEqual(provider.model_name, 'deepseek-flash')
        self.assertFalse(provider.model_replacement)

    def test_current_model_has_no_warning(self):
        provider = self.env['os.ai.provider'].create({
            'name': 'Current', 'provider_type': 'anthropic', 'model_name': 'claude-sonnet-5-5',
        })
        self.assertFalse(provider.model_status_message)


@tagged('post_install', '-at_install')
class TestImageInput(TransactionCase):
    """Images are sent with their real media type (Anthropic rejects a mismatch)."""

    @staticmethod
    def b64(raw):
        import base64
        return base64.b64encode(raw).decode()

    def test_media_types(self):
        url = ai_provider_module.image_data_url
        self.assertTrue(url(self.b64(b'\x89PNG\r\n\x1a\n' + b'\0' * 30)).startswith('data:image/png;base64,'))
        self.assertTrue(url(self.b64(b'\xff\xd8\xff\xe0' + b'\0' * 30)).startswith('data:image/jpeg;base64,'))
        self.assertTrue(url(self.b64(b'GIF89a' + b'\0' * 30)).startswith('data:image/gif;base64,'))
        self.assertTrue(url(self.b64(b'RIFF\0\0\0\0WEBPVP8 ' + b'\0' * 30)).startswith('data:image/webp;base64,'))
        self.assertEqual(url('data:image/png;base64,AAAA'), 'data:image/png;base64,AAAA')
        with self.assertRaises(ValueError):
            url(self.b64(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'))

    def test_vision_call_declares_png(self):
        provider = self.env['os.ai.provider'].create({
            'name': 'Vision', 'provider_type': 'anthropic', 'model_name': 'claude-haiku-4-5', 'capable_vision': True,
        })
        png = self.b64(b'\x89PNG\r\n\x1a\n' + b'\0' * 30)
        with patch.object(ai_provider_module.litellm, 'completion', return_value=fake_completion()) as completion:
            provider.call_llm('system', 'Describe [IMAGE]', images=[png], capability_used='capable_vision')
        parts = completion.call_args.kwargs['messages'][-1]['content']
        self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_svg_is_a_permanent_error(self):
        provider = self.env['os.ai.provider'].create({
            'name': 'Vision', 'provider_type': 'openai', 'model_name': 'gpt-6-luna', 'capable_vision': True,
        })
        svg = self.b64(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with patch.object(ai_provider_module.litellm, 'completion') as completion:
            result = provider.call_llm('system', 'Describe [IMAGE]', images=[svg], capability_used='capable_vision')
        completion.assert_not_called()
        self.assertEqual(result['error_kind'], 'permanent')
