# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.os_ai_fields.models import ai_field_mixin


@tagged('post_install', '-at_install')
class TestPromptSync(TransactionCase):
    """Prompts edited from the UI survive registry reloads."""

    def setUp(self):
        super().setUp()
        self.prompt = self.env['os.ai.prompt'].search([
            ('model_id.model', '=', 'res.partner'),
            ('field_name', '=', 'ai_greeting'),
        ])
        self.code_prompt = self.env['res.partner']._fields['ai_greeting'].ai_compute

    def resync(self):
        ai_field_mixin._ai_prompts_synced.discard(id(self.env.registry))
        ai_field_mixin._sync_ai_prompts(self.env)

    def test_prompt_exists(self):
        self.assertEqual(len(self.prompt), 1)
        self.assertEqual(self.prompt.default_prompt_text, self.code_prompt)

    def test_custom_prompt_preserved(self):
        self.prompt.prompt_text = 'CUSTOM greeting prompt for {name}'
        self.resync()
        self.assertEqual(self.prompt.prompt_text, 'CUSTOM greeting prompt for {name}')

    def test_restore_default(self):
        self.prompt.prompt_text = 'CUSTOM'
        self.prompt.action_restore_default()
        self.assertEqual(self.prompt.prompt_text, self.code_prompt)

    def test_code_update_applies_to_non_customized_prompt(self):
        # Simulate a prompt synced from an older version of the code
        self.prompt.write({'prompt_text': 'OLD DEFAULT', 'default_prompt_text': 'OLD DEFAULT'})
        self.resync()
        self.assertEqual(self.prompt.prompt_text, self.code_prompt)
        self.assertEqual(self.prompt.default_prompt_text, self.code_prompt)

    def test_code_update_keeps_customized_prompt(self):
        self.prompt.write({'prompt_text': 'CUSTOM', 'default_prompt_text': 'OLD DEFAULT'})
        self.resync()
        self.assertEqual(self.prompt.prompt_text, 'CUSTOM')
        self.assertEqual(self.prompt.default_prompt_text, self.code_prompt)
