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
    sequence = fields.Integer('Sequence', default=10)
    active = fields.Boolean('Active', default=True)

    ai_log_count = fields.Integer('Log Count', compute='_compute_ai_log_count')

    @api.depends()
    def _compute_ai_log_count(self):
        AiLog = self.env['os.ai.log']
        for rec in self:
            rec.ai_log_count = AiLog.search_count([
                ('model_name', '=', rec.model_id.model),
                ('field_name', '=', rec.field_name),
            ])

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'AI Logs',
            'res_model': 'os.ai.log',
            'view_mode': 'tree,form',
            'domain': [
                ('model_name', '=', self.model_id.model),
                ('field_name', '=', self.field_name),
            ],
            'context': {'default_model_name': self.model_id.model, 'default_field_name': self.field_name},
        }
