# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.os_ai_fields.compat import set_param

from .common import AiFieldsCase, batch_answer, flush_all, llm_error, llm_ok


@tagged('post_install', '-at_install')
class TestCronIntegrity(AiFieldsCase):
    """Failed or invalid LLM answers must never wipe values or finish records."""

    def setUp(self):
        super().setUp()
        self.p1 = self.Partner.create({'name': 'Cron One'})
        self.p2 = self.Partner.create({'name': 'Cron Two'})
        self.partners = self.p1 | self.p2
        self.reset_pending()
        self.set_sql(self.partners, ai_greeting='OLD', ai_greeting_pending=True)

    def greeting_state(self):
        return self.sql_values(self.partners, 'ai_greeting', 'ai_greeting_pending')

    def test_successful_batch(self):
        with self.mock_llm(lambda prompt, fmt: batch_answer(prompt, lambda rid: 'Hello %s' % rid)) as calls:
            self.run_cron()
        self.assertEqual(len(calls), 1)
        state = self.greeting_state()
        self.assertEqual(state[self.p1.id], ('Hello %s' % self.p1.id, False))
        self.assertEqual(state[self.p2.id], ('Hello %s' % self.p2.id, False))

    def test_api_failure_keeps_value_and_pending(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('429 Too Many Requests')):
            self.run_cron()
        state = self.greeting_state()
        self.assertEqual(state[self.p1.id], ('OLD', True))
        self.assertEqual(state[self.p2.id], ('OLD', True))

    def test_invalid_json_keeps_values(self):
        with self.mock_llm(lambda prompt, fmt: llm_ok('this is not JSON')):
            self.run_cron()
        state = self.greeting_state()
        self.assertEqual(state[self.p1.id], ('OLD', True))
        self.assertEqual(state[self.p2.id], ('OLD', True))

    def test_partial_batch_only_updates_answered_records(self):
        answer = llm_ok(json.dumps({'results': [{'id': str(self.p1.id), 'value': 'NEW'}]}))
        with self.mock_llm(lambda prompt, fmt: answer):
            self.run_cron()
        state = self.greeting_state()
        self.assertEqual(state[self.p1.id], ('NEW', False))
        self.assertEqual(state[self.p2.id], ('OLD', True))

    def test_duplicate_ids_are_not_trusted(self):
        answer = llm_ok(json.dumps({'results': [
            {'id': str(self.p1.id), 'value': 'A'},
            {'id': str(self.p1.id), 'value': 'B'},
            {'id': str(self.p2.id), 'value': 'NEW'},
        ]}))
        with self.mock_llm(lambda prompt, fmt: answer):
            self.run_cron()
        state = self.greeting_state()
        self.assertEqual(state[self.p1.id], ('OLD', True))
        self.assertEqual(state[self.p2.id], ('NEW', False))

    def test_single_record_empty_answer_keeps_value(self):
        self.set_sql(self.p2, ai_greeting_pending=False)
        with self.mock_llm(lambda prompt, fmt: llm_ok('')):
            self.run_cron()
        self.assertEqual(self.greeting_state()[self.p1.id], ('OLD', True))

    def test_single_record_malformed_json_keeps_value(self):
        self.set_sql(self.p2, ai_greeting_pending=False)
        with self.mock_llm(lambda prompt, fmt: llm_ok('{"value" : "cut in the midd')):
            self.run_cron()
        self.assertEqual(self.greeting_state()[self.p1.id], ('OLD', True))

    def test_single_record_plain_text_is_accepted(self):
        # models without structured output may answer the plain value
        self.set_sql(self.p2, ai_greeting_pending=False)
        with self.mock_llm(lambda prompt, fmt: llm_ok('Hello plain text')):
            self.run_cron()
        self.assertEqual(self.greeting_state()[self.p1.id], ('Hello plain text', False))

    def test_single_record_explicit_value(self):
        self.set_sql(self.p2, ai_greeting_pending=False)
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'Single NEW'}))):
            self.run_cron()
        self.assertEqual(self.greeting_state()[self.p1.id], ('Single NEW', False))


@tagged('post_install', '-at_install')
class TestComputeStatus(AiFieldsCase):
    """Failures are persisted per record/field and drive retries."""

    def setUp(self):
        super().setUp()
        self.partner = self.Partner.create({'name': 'Status partner'})
        self.reset_pending()
        self.set_sql(self.partner, ai_greeting='OLD', ai_greeting_pending=True)
        self.Status = self.env['os.ai.compute.status']

    def status(self):
        return self.Status.search([
            ('model_name', '=', 'res.partner'),
            ('field_name', '=', 'ai_greeting'),
            ('res_id', '=', self.partner.id),
        ])

    def test_retryable_failure_schedules_retry(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('503 Service Unavailable')):
            self.run_cron()
        status = self.status()
        self.assertEqual(status.state, 'retry')
        self.assertEqual(status.attempts, 1)
        self.assertTrue(status.next_attempt)
        self.assertIn('503', status.error_message)

    def test_retry_not_due_is_skipped(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('timeout')):
            self.run_cron()
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'NEW'}))) as calls:
            self.run_cron()
        self.assertFalse(calls)
        self.assertEqual(self.sql_values(self.partner, 'ai_greeting')[self.partner.id], ('OLD',))

    def test_permanent_failure_blocks_until_manual_retry(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('401 invalid api key', kind='permanent')):
            self.run_cron()
        self.assertEqual(self.status().state, 'failed')
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'NEW'}))) as calls:
            self.run_cron()
        self.assertFalse(calls)

        self.status().action_retry()
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'NEW'}))) as calls:
            self.run_cron()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sql_values(self.partner, 'ai_greeting', 'ai_greeting_pending')[self.partner.id],
                         ('NEW', False))
        self.assertFalse(self.status())

    def test_dependency_change_clears_failure(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('401 invalid api key', kind='permanent')):
            self.run_cron()
        self.assertTrue(self.status())
        self.partner.write({'name': 'Status partner (renamed)'})
        flush_all(self.env)  # recomputation (and thus pending marking) happens at flush
        self.assertFalse(self.status())
        self.assertEqual(self.sql_values(self.partner, 'ai_greeting_pending')[self.partner.id], (True,))

    def test_success_clears_status(self):
        with self.mock_llm(lambda prompt, fmt: llm_error('timeout')):
            self.run_cron()
        self.status().write({'next_attempt': '2000-01-01 00:00:00'})
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'NEW'}))):
            self.run_cron()
        self.assertFalse(self.status())
        self.assertEqual(self.sql_values(self.partner, 'ai_greeting')[self.partner.id], ('NEW',))


@tagged('post_install', '-at_install')
class TestCronBudgetAndSchemas(AiFieldsCase):

    def setUp(self):
        super().setUp()
        self.partner = self.Partner.create({'name': 'Budget partner', 'is_company': True})
        self.reset_pending()

    def test_time_budget_leaves_records_pending(self):
        self.set_sql(self.partner, ai_greeting='OLD', ai_greeting_pending=True)
        set_param(self.env, 'os_ai_fields.cron_time_budget', '0.000001')  # exhausted right after the start
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'NEW'}))) as calls:
            self.run_cron()
        self.assertFalse(calls)
        self.assertEqual(self.sql_values(self.partner, 'ai_greeting', 'ai_greeting_pending')[self.partner.id],
                         ('OLD', True))

    def test_selection_is_requested_as_an_enum(self):
        self.set_sql(self.partner, ai_partner_type_suggestion_pending=True)
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'supplier'}))) as calls:
            self.run_cron()
        self.assertEqual(len(calls), 1)
        value_schema = calls[0]['response_format']['json_schema']['schema']['properties']['value']
        self.assertEqual(value_schema['enum'], ['prospect', 'customer', 'supplier', 'partner', 'other', ''])
        self.assertIn('- supplier: Supplier', calls[0]['user_prompt'])
        self.assertEqual(self.sql_values(self.partner, 'ai_partner_type_suggestion')[self.partner.id], ('supplier',))

    def test_selection_label_is_mapped_to_its_key(self):
        self.set_sql(self.partner, ai_partner_type_suggestion_pending=True)
        with self.mock_llm(lambda prompt, fmt: llm_ok(json.dumps({'value': 'Supplier'}))):
            self.run_cron()
        self.assertEqual(self.sql_values(self.partner, 'ai_partner_type_suggestion')[self.partner.id], ('supplier',))


@tagged('post_install', '-at_install')
class TestFieldConfigurationErrors(AiFieldsCase):
    """A configuration error of a field is visible in AI Computation Errors."""

    def test_configuration_error_line(self):
        partner = self.Partner.create({'name': 'Configuration error'})
        self.reset_pending()
        self.set_sql(partner, ai_greeting_pending=True)
        field = self.Partner._fields['ai_greeting']
        Status = self.env['os.ai.compute.status']
        domain = [('model_name', '=', 'res.partner'), ('field_name', '=', 'ai_greeting'), ('res_id', '=', 0)]
        with patch.object(field, 'ai_compute_engine', 'typo-engine'), \
                self.mock_llm(lambda prompt, fmt: llm_ok('{"value": "Hello"}')) as calls:
            self.run_cron()
        self.assertFalse(calls)
        line = Status.search(domain)
        self.assertEqual(line.state, 'failed')
        self.assertIn('typo-engine', line.error_message)
        with self.assertRaises(UserError):
            line.action_open_record()
        # fixed configuration: the line disappears and the record is computed
        with self.mock_llm(lambda prompt, fmt: llm_ok('{"value": "Hello"}')):
            self.run_cron()
        self.assertFalse(Status.search(domain))
        self.assertEqual(self.sql_values(partner, 'ai_greeting', 'ai_greeting_pending')[partner.id], ('Hello', False))
