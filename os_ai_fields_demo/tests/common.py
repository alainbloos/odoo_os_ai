# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
import re
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase

BATCH_ID_RE = re.compile(r'- ID "(\d+)"')


def flush_all(env):
    if hasattr(env, 'flush_all'):
        env.flush_all()
    else:
        env['base'].flush()


def invalidate_all(env):
    if hasattr(env, 'invalidate_all'):
        env.invalidate_all()
    else:
        env['base'].invalidate_cache()


def llm_ok(content):
    return {
        'content': content,
        'raw_response': {},
        'prompt_tokens': 10,
        'completion_tokens': 5,
        'total_tokens': 15,
        'cost': 0.0,
        'model': 'openai/test-model',
        'duration': 0.01,
        'success': True,
        'error': None,
    }


def llm_error(error, kind='retryable'):
    result = llm_ok('')
    result.update(success=False, error=error, error_kind=kind)
    return result


def batch_ids(user_prompt):
    """Return the record IDs listed in a batch prompt, as strings."""
    return BATCH_ID_RE.findall(user_prompt)


def batch_answer(user_prompt, value_for):
    """Build a well-formed batch response: value_for(record_id_str) -> value."""
    return llm_ok(json.dumps({
        'results': [{'id': rid, 'value': value_for(rid)} for rid in batch_ids(user_prompt)],
    }))


class AiFieldsCase(TransactionCase):
    """Common setup: one active text provider and helpers to drive the cron."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']
        cls.env['os.ai.provider'].search([]).write({'active': False})
        cls.provider = cls.env['os.ai.provider'].create({
            'name': 'Test provider',
            'provider_type': 'openai',
            'model_name': 'test-model',
            'sequence': 1,
            'capable_text': True,
            'capable_vision': True,
        })
        cls.async_ai_fields = [
            name for name, field in cls.Partner._fields.items()
            if getattr(field, 'ai_compute', None) and getattr(field, 'ai_compute_async', True)
            and '%s_pending' % name in cls.Partner._fields
        ]

    # ------------------------------------------------------------------
    # Database helpers (read what will really be committed)
    # ------------------------------------------------------------------

    def sql_values(self, records, *columns):
        flush_all(self.env)
        self.env.cr.execute(
            'SELECT id, %s FROM %s WHERE id IN %%s' % (', '.join(columns), records._table),
            [tuple(records.ids)],
        )
        return {row[0]: row[1:] for row in self.env.cr.fetchall()}

    def set_sql(self, records, **values):
        flush_all(self.env)
        assignments = ', '.join('%s = %%s' % column for column in values)
        self.env.cr.execute(
            'UPDATE %s SET %s WHERE id IN %%s' % (records._table, assignments),
            list(values.values()) + [tuple(records.ids)],
        )
        invalidate_all(self.env)

    def reset_pending(self):
        """Clear every pending flag so that only the records a test marks are processed."""
        flush_all(self.env)
        columns = ', '.join('%s_pending = false' % name for name in self.async_ai_fields)
        self.env.cr.execute('UPDATE res_partner SET %s' % columns)
        invalidate_all(self.env)

    # ------------------------------------------------------------------
    # LLM / cron helpers
    # ------------------------------------------------------------------

    @contextmanager
    def mock_llm(self, responder):
        """Replace os.ai.provider.call_llm; responder(user_prompt, response_format) -> result."""
        calls = []

        def fake_call_llm(provider, system_prompt, user_prompt, response_format=None,
                          images=None, capability_used='capable_text', **kwargs):
            calls.append({
                'provider': provider,
                'user_prompt': user_prompt,
                'response_format': response_format,
                'capability_used': capability_used,
            })
            return responder(user_prompt, response_format)

        Provider = type(self.env['os.ai.provider'])
        with patch.object(Provider, 'call_llm', autospec=True, side_effect=fake_call_llm):
            yield calls

    @contextmanager
    def mock_transaction_control(self):
        """The cron commits per batch; inside a test we only flush instead."""
        counter = {'commit': 0, 'rollback': 0}

        def fake_commit():
            flush_all(self.env)
            counter['commit'] += 1

        def fake_rollback():
            counter['rollback'] += 1

        with patch.object(self.env.cr, 'commit', fake_commit), \
                patch.object(self.env.cr, 'rollback', fake_rollback):
            yield counter

    def run_cron(self):
        with self.mock_transaction_control() as counter:
            self.env['ai.field.mixin']._cron_compute_os_ai_fields()
        return counter
