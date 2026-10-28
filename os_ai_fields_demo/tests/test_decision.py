# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.os_ai_fields import patch as ai_patch

from .common import AiFieldsCase, llm_ok

SOUTH_AMERICA = ['AR', 'BO', 'BR', 'CL', 'CO', 'EC', 'GY', 'PY', 'PE', 'SR', 'UY', 'VE']


def decision_ok(answer):
    return {
        'content': json.dumps({'value': answer}), 'answers': {'value': answer}, 'raw_response': {},
        'prompt_tokens': 300, 'completion_tokens': 10, 'total_tokens': 310, 'cost': 0.0000126,
        'cost_status': 'estimated', 'model': 'jev-1.13.0', 'duration': 0.2,
        'success': True, 'error': None, 'error_kind': None,
    }


@tagged('post_install', '-at_install')
class TestDecisionEngine(AiFieldsCase):
    """Fields with ai_compute_engine='decision' use a typed decision model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.decision_provider = cls.env['os.ai.provider'].create({
            'name': 'Decision provider', 'provider_type': 'typesafe', 'model_name': 'jev-1.13.0',
            'sequence': 2, 'capable_text': False, 'capable_decision': True,
        })

    def setUp(self):
        super().setUp()
        self.partner = self.Partner.create({
            'name': 'Unique Name 7Q', 'function': 'Purchasing manager', 'city': 'Montevideo',
        })
        self.reset_pending()

    @contextmanager
    def mock_decision(self, answer_for):
        calls = []

        def fake_call_decision(provider, state, questions):
            calls.append({'provider': provider, 'state': state, 'questions': questions})
            return answer_for(state, questions)

        Provider = type(self.env['os.ai.provider'])
        with patch.object(Provider, 'call_decision', autospec=True, side_effect=fake_call_decision):
            yield calls

    def compute(self, field_name, answer_for):
        self.set_sql(self.partner, **{'%s_pending' % field_name: True})
        with self.mock_decision(answer_for) as calls, \
                self.mock_llm(lambda prompt, fmt: llm_ok('{"value": "should not be called"}')) as llm_calls:
            self.run_cron()
        self.assertFalse(llm_calls, 'decision fields must not use the text LLM')
        return calls

    def stored(self, field_name):
        return self.sql_values(self.partner, field_name, '%s_pending' % field_name)[self.partner.id]

    def status(self, field_name):
        return self.env['os.ai.compute.status'].search([
            ('model_name', '=', 'res.partner'), ('field_name', '=', field_name), ('res_id', '=', self.partner.id),
        ])

    # -- request -------------------------------------------------------------

    def test_instructions_reference_the_state(self):
        calls = self.compute('ai_is_b2b', lambda s, q: decision_ok({'type': 'noul', 'noul': 0.9}))
        self.assertEqual(len(calls), 1)
        question = calls[0]['questions']['value']
        self.assertEqual(question['type'], 'noul')
        self.assertIn('`name`', question['instructions'])
        self.assertNotIn('Unique Name 7Q', question['instructions'])  # no record data in the instructions
        self.assertEqual(calls[0]['state']['name'], 'Unique Name 7Q')
        self.assertEqual(calls[0]['state']['function'], 'Purchasing manager')
        self.assertEqual(calls[0]['provider'], self.decision_provider)

    # -- Boolean (noul) ------------------------------------------------------

    def test_boolean_yes(self):
        self.compute('ai_is_b2b', lambda s, q: decision_ok({'type': 'noul', 'noul': 0.9}))
        self.assertEqual(self.stored('ai_is_b2b'), (True, False))

    def test_boolean_no(self):
        self.set_sql(self.partner, ai_is_b2b=True)
        self.compute('ai_is_b2b', lambda s, q: decision_ok({'type': 'noul', 'noul': 0.1}))
        self.assertEqual(self.stored('ai_is_b2b'), (False, False))

    def test_boolean_uncertain_keeps_value(self):
        self.set_sql(self.partner, ai_is_b2b=True)
        self.compute('ai_is_b2b', lambda s, q: decision_ok({'type': 'noul', 'noul': 0.55}))
        self.assertEqual(self.stored('ai_is_b2b'), (True, False))  # kept, and not retried
        status = self.status('ai_is_b2b')
        self.assertEqual(status.state, 'abstained')
        self.assertIn('0.55', status.error_message)

    # -- Selection (choice) --------------------------------------------------

    def test_selection_choice(self):
        calls = self.compute('ai_decision_partner_type', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': 'supplier', 'confidence': 0.9,
             'probabilities': {'supplier': 0.95, 'customer': 0.05}}))
        criteria = calls[0]['questions']['value']['criteria']
        self.assertEqual(list(criteria), ['prospect', 'customer', 'supplier', 'partner', 'other', '__none__'])
        self.assertEqual(criteria['supplier'], 'Supplier')
        self.assertEqual(self.stored('ai_decision_partner_type'), ('supplier', False))

    def test_selection_none_option(self):
        self.set_sql(self.partner, ai_decision_partner_type='customer')
        self.compute('ai_decision_partner_type', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': '__none__', 'confidence': 0.9}))
        self.assertEqual(self.stored('ai_decision_partner_type'), (None, False))

    def test_selection_low_confidence_keeps_value(self):
        self.set_sql(self.partner, ai_decision_partner_type='customer')
        self.compute('ai_decision_partner_type', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': 'supplier', 'confidence': 0.4}))  # field minimum: 0.6
        self.assertEqual(self.stored('ai_decision_partner_type'), ('customer', False))
        self.assertEqual(self.status('ai_decision_partner_type').state, 'abstained')

    def test_unknown_option_is_an_invalid_answer(self):
        self.compute('ai_decision_partner_type', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': 'reseller', 'confidence': 0.99}))
        self.assertEqual(self.stored('ai_decision_partner_type'), (None, True))
        self.assertEqual(self.status('ai_decision_partner_type').state, 'retry')

    # -- Many2one (choice over the domain) -------------------------------------

    def test_many2one_candidates(self):
        uruguay = self.env.ref('base.uy')
        calls = self.compute('ai_decision_country', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': str(uruguay.id), 'confidence': 0.8}))
        criteria = calls[0]['questions']['value']['criteria']
        expected = self.env['res.country'].search([('code', 'in', SOUTH_AMERICA)])
        self.assertEqual(set(criteria) - {'__none__'}, {str(i) for i in expected.ids})
        self.assertEqual(criteria[str(uruguay.id)], uruguay.display_name)
        self.assertEqual(self.stored('ai_decision_country'), (uruguay.id, False))

    def test_many2one_option_outside_domain_is_rejected(self):
        france = self.env.ref('base.fr')
        self.compute('ai_decision_country', lambda s, q: decision_ok(
            {'type': 'choice', 'choice': str(france.id), 'confidence': 0.99}))
        self.assertEqual(self.stored('ai_decision_country'), (None, True))

    # -- routing --------------------------------------------------------------

    def test_llm_fields_ignore_the_decision_provider(self):
        self.set_sql(self.partner, ai_greeting_pending=True)
        with self.mock_decision(lambda s, q: decision_ok({'type': 'noul', 'noul': 1.0})) as calls, \
                self.mock_llm(lambda prompt, fmt: llm_ok('{"value": "Hello"}')) as llm_calls:
            self.run_cron()
        self.assertFalse(calls)
        self.assertEqual(len(llm_calls), 1)
        self.assertEqual(llm_calls[0]['provider'], self.provider)

    def test_no_decision_provider_leaves_the_field_pending(self):
        self.decision_provider.active = False
        self.set_sql(self.partner, ai_is_b2b_pending=True)
        with self.mock_llm(lambda prompt, fmt: llm_ok('{"value": true}')) as llm_calls:
            self.run_cron()
        self.assertFalse(llm_calls)
        self.assertEqual(self.stored('ai_is_b2b'), (None, True))

    def test_unsupported_field_type(self):
        field = self.Partner._fields['ai_greeting']
        with patch.object(field, 'ai_compute_engine', 'decision'):
            plan, reason = ai_patch.prepare_computation(self.env, 'res.partner', 'ai_greeting', field, 1)
        self.assertIsNone(plan)
        self.assertIn('Boolean, Selection and Many2one', reason)

    # -- answer validation -------------------------------------------------------

    def test_out_of_range_probability_keeps_value(self):
        self.set_sql(self.partner, ai_is_b2b=False)
        self.compute('ai_is_b2b', lambda s, q: decision_ok({'type': 'noul', 'noul': 2}))
        self.assertEqual(self.stored('ai_is_b2b'), (False, True))
        self.assertEqual(self.status('ai_is_b2b').state, 'retry')

    def test_invalid_answers_are_rejected(self):
        boolean = self.Partner._fields['ai_is_b2b']
        selection = self.Partner._fields['ai_decision_partner_type']
        options = {'supplier': 'supplier'}
        for answer in ({'noul': 2}, {'noul': -0.1}, {'noul': float('nan')}, {'noul': float('inf')},
                       {'noul': '0.9'}, {'noul': True}, {}, 'yes'):
            with self.assertRaises(ValueError, msg=repr(answer)):
                ai_patch._interpret_decision(boolean, answer, None)
        for answer in ({'choice': 'x', 'confidence': 0.9}, {'choice': 'supplier', 'confidence': float('nan')},
                       {'choice': 'supplier', 'confidence': 1.5}, {'choice': 'supplier'}, {'choice': None, 'confidence': 1}):
            with self.assertRaises(ValueError, msg=repr(answer)):
                ai_patch._interpret_decision(selection, answer, options)

    def test_probability_bounds_are_valid(self):
        boolean = self.Partner._fields['ai_is_b2b']
        self.assertEqual(ai_patch._interpret_decision(boolean, {'noul': 1}, None)[:2], ('done', True))
        self.assertEqual(ai_patch._interpret_decision(boolean, {'noul': 0.0}, None)[:2], ('done', False))

    def test_invalid_decision_parameters(self):
        field = self.Partner._fields['ai_is_b2b']
        for param, value in (('ai_decision_margin', 0.6), ('ai_decision_threshold', 1.5),
                             ('ai_decision_min_confidence', -1)):
            with patch.object(field, param, value):
                plan, reason = ai_patch.prepare_computation(self.env, 'res.partner', 'ai_is_b2b', field, 1)
            self.assertIsNone(plan)
            self.assertIn('invalid decision parameters', reason)
