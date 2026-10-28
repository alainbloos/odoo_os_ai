# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged('post_install', '-at_install')
class TestOsAiSecurity(TransactionCase):
    """API keys and logs (full prompts/responses) are reserved to administrators."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='os_ai_internal_user', groups='base.group_user')
        cls.admin = new_test_user(cls.env, login='os_ai_admin_user', groups='base.group_user,base.group_system')
        cls.provider = cls.env['os.ai.provider'].create({
            'name': 'Secret provider',
            'provider_type': 'openai',
            'model_name': 'test-model',
            'api_key': 'sk-test-secret',
        })
        cls.log = cls.env['os.ai.log'].create({
            'model_name': 'res.partner',
            'field_name': 'name',
            'user_prompt': 'confidential prompt',
            'response_content': 'confidential answer',
        })

    def test_internal_user_cannot_read_api_key(self):
        with self.assertRaises(AccessError):
            self.provider.with_user(self.user).read(['api_key'])

    def test_internal_user_can_read_provider_metadata(self):
        data = self.provider.with_user(self.user).read(['name', 'capable_text'])
        self.assertEqual(data[0]['name'], 'Secret provider')

    def test_admin_can_read_api_key(self):
        data = self.provider.with_user(self.admin).read(['api_key'])
        self.assertEqual(data[0]['api_key'], 'sk-test-secret')

    def test_internal_user_cannot_read_logs(self):
        with self.assertRaises(AccessError):
            self.log.with_user(self.user).read(['user_prompt'])

    def test_admin_can_read_logs(self):
        data = self.log.with_user(self.admin).read(['user_prompt'])
        self.assertEqual(data[0]['user_prompt'], 'confidential prompt')

    def test_log_call_does_not_elevate_privileges(self):
        """log_call() is reachable through RPC: it must not create logs for users who cannot."""
        Log = self.env['os.ai.log']
        count = Log.search_count([])
        portal = new_test_user(self.env, login='os_ai_portal_user', groups='base.group_portal')
        self.assertFalse(Log.with_user(portal).log_call({'model_name': 'res.partner', 'field_name': 'name'}))
        self.assertFalse(Log.with_user(self.user).log_call({'model_name': 'res.partner', 'field_name': 'name'}))
        self.assertEqual(Log.search_count([]), count)
        # code logging on behalf of a user does it explicitly
        self.assertTrue(Log.with_user(self.user).sudo().log_call({'model_name': 'res.partner', 'field_name': 'name'}))
