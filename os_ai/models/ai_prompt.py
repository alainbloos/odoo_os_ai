# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, fields, models


class AiPrompt(models.Model):
    _name = 'os.ai.prompt'
    _description = 'AI Prompt'
    _order = 'sequence, id'

    name = fields.Char('Name', required=True)
    model_id = fields.Many2one(
        'ir.model', string='Model', required=True, ondelete='cascade',
        help="The Odoo model this prompt is associated with.",
    )
    field_name = fields.Char(
        'Field Name', required=True,
        help="Technical name of the target field (e.g. ai_summary).",
    )
    prompt_text = fields.Text(
        'Prompt Template', required=True,
        help="Prompt template with {field} placeholders. Supports dot-notation "
             "(e.g. {partner_id.name}) and system variables ({__date__}, "
             "{__user__}, {__company__}).",
    )
    default_prompt_text = fields.Text(
        'Default Prompt', readonly=True,
        help="Prompt defined in the code (ai_compute parameter). Updated on every "
             "registry load; used to restore the prompt and to detect customizations.",
    )
    is_customized = fields.Boolean(
        'Customized', compute='_compute_is_customized',
        help="The prompt was edited and differs from the default defined in the code.",
    )
    sequence = fields.Integer('Sequence', default=10)
    active = fields.Boolean('Active', default=True)

    ai_log_count = fields.Integer('Log Count', compute='_compute_ai_log_count')

    @api.depends('prompt_text', 'default_prompt_text')
    def _compute_is_customized(self):
        for rec in self:
            rec.is_customized = bool(rec.default_prompt_text) and rec.prompt_text != rec.default_prompt_text

    @api.depends()
    def _compute_ai_log_count(self):
        # Logs hold full prompts/responses and are restricted to administrators;
        # the count alone is not sensitive.
        AiLog = self.env['os.ai.log'].sudo()
        for rec in self:
            rec.ai_log_count = AiLog.search_count([
                ('model_name', '=', rec.model_id.model),
                ('field_name', '=', rec.field_name),
            ])

    def action_restore_default(self):
        """Replace the prompt with the default defined in the code."""
        for rec in self.filtered('default_prompt_text'):
            rec.prompt_text = rec.default_prompt_text

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'AI Logs',
            'res_model': 'os.ai.log',
            'view_mode': 'list,form',
            'domain': [
                ('model_name', '=', self.model_id.model),
                ('field_name', '=', self.field_name),
            ],
            'context': {'default_model_name': self.model_id.model, 'default_field_name': self.field_name},
        }
