# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, fields, models

DEFAULT_SYSTEM_PROMPT = (
    "You are a field computation assistant for an Odoo ERP system. "
    "Your task is to generate values for specific fields based on the instructions provided. "
    "Respond ONLY with the value for the field — no explanations, no labels, no extra text. "
    "Be concise, accurate, and follow the language of the instruction."
)

PARAM_KEY = 'os_ai_fields.system_prompt'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    os_ai_fields_system_prompt = fields.Text(
        string="AI System Prompt",
        help="General instructions sent to the LLM before every field-specific prompt. "
             "This sets the context and behavior for the AI assistant.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        res['os_ai_fields_system_prompt'] = self.env['ir.config_parameter'].sudo().get_param(
            PARAM_KEY, DEFAULT_SYSTEM_PROMPT,
        )
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            PARAM_KEY, self.os_ai_fields_system_prompt or DEFAULT_SYSTEM_PROMPT,
        )
