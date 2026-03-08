# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiLog(models.Model):
    _name = 'os.ai.log'
    _description = 'AI Field Computation Log'
    _order = 'create_date desc, id desc'

    # -- Context --
    model_name = fields.Char('Model', required=True, index=True)
    field_name = fields.Char('Field', required=True, index=True)
    res_ids = fields.Char(
        'Record IDs',
        help="JSON list of record IDs involved in this LLM call",
    )
    provider_id = fields.Many2one('os.ai.provider', string='Provider', ondelete='set null')
    provider_model = fields.Char('LLM Model', help="Model identifier sent to litellm")

    # -- Prompts --
    system_prompt = fields.Text('System Prompt')
    user_prompt = fields.Text('User Prompt')

    # -- Response --
    response_content = fields.Text('Response Content')
    response_raw = fields.Text('Raw Response (JSON)', help="Full litellm response serialized as JSON")
    success = fields.Boolean('Success', default=True)
    error_message = fields.Text('Error Message')

    # -- Usage / Cost --
    prompt_tokens = fields.Integer('Prompt Tokens')
    completion_tokens = fields.Integer('Completion Tokens')
    total_tokens = fields.Integer('Total Tokens')
    response_cost = fields.Float('Cost (USD)', digits=(12, 8))

    # -- Timing --
    duration = fields.Float('Duration (s)', digits=(8, 3))
    batch_size = fields.Integer('Batch Size', help="Number of records in this batch")

    # -- Helpers --
    @api.model
    def log_call(self, vals):
        """Create a log entry. Designed to be called from compute methods.

        Args:
            vals (dict): Log values to record.

        Returns:
            ai.log record
        """
        try:
            return self.sudo().create(vals)
        except Exception as exc:
            _logger.error("os_ai: failed to create ai.log entry: %s", exc)
            return self.browse()
