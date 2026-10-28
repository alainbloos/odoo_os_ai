# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Durable failure state of async AI computations, per record and field.

A record whose computation failed keeps its previous value and its
``{field}_pending`` flag.  This model tells the cron when to try again:

* retryable failures (rate limit, timeout, invalid answer...) are retried
  with an increasing delay, up to MAX_ATTEMPTS;
* permanent failures (bad credentials, unknown model, value rejected by the
  database...) and exhausted retries are not retried until a user asks for
  it (action_retry) or a dependency of the field changes;
* abstentions of a decision model (answer not confident enough) keep the
  previous value and are only recorded.

Successful computations remove the row.
"""

import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.os_ai.models.ai_provider import ERROR_PERMANENT

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# A configuration error of the field itself (not of a record) is stored with res_id 0
FIELD_ERROR_RES_ID = 0
RETRY_DELAYS_MINUTES = (5, 15, 60, 240)
ABSTAINED = 'abstained'  # same outcome as os_ai_fields.patch.ABSTAINED


class AiComputeStatus(models.Model):
    _name = 'os.ai.compute.status'
    _description = 'AI Field Computation Status'
    _order = 'write_date desc, id desc'
    _rec_name = 'field_name'

    model_name = fields.Char('Model', required=True, index=True, readonly=True)
    res_id = fields.Integer('Record ID', required=True, index=True, readonly=True)
    field_name = fields.Char('Field', required=True, index=True, readonly=True)
    state = fields.Selection([
        ('retry', 'Retry scheduled'),
        ('failed', 'Failed'),
        ('abstained', 'Low confidence'),
    ], string='Status', required=True, default='retry', readonly=True)
    attempts = fields.Integer('Attempts', readonly=True)
    next_attempt = fields.Datetime('Next Attempt', readonly=True)
    error_message = fields.Text('Last Error', readonly=True)

    @api.model
    def _get_blocked_ids(self, model_name, field_name):
        """IDs the cron must skip: failed, or waiting for their next attempt."""
        rows = self.search([
            ('model_name', '=', model_name),
            ('field_name', '=', field_name),
            '|', ('state', '=', 'failed'), ('next_attempt', '>', fields.Datetime.now()),
        ])
        return set(rows.mapped('res_id'))

    @api.model
    def _clear(self, model_name, field_name, res_ids):
        """Forget the failures of these records (computed, or inputs changed)."""
        if not res_ids:
            return
        self.search([
            ('model_name', '=', model_name),
            ('field_name', '=', field_name),
            ('res_id', 'in', list(res_ids)),
        ]).unlink()

    @api.model
    def _record_failures(self, model_name, field_name, failures):
        """Store failed computations.

        Args:
            failures (dict): {res_id: (error_kind, message)}
        """
        if not failures:
            return
        existing = {
            status.res_id: status
            for status in self.search([
                ('model_name', '=', model_name),
                ('field_name', '=', field_name),
                ('res_id', 'in', list(failures)),
            ])
        }
        now = fields.Datetime.now()
        to_create = []
        for res_id, (error_kind, message) in failures.items():
            status = existing.get(res_id)
            attempts = (status.attempts if status else 0) + 1
            if error_kind == ABSTAINED:
                state, next_attempt = 'abstained', False
            elif error_kind == ERROR_PERMANENT or attempts >= MAX_ATTEMPTS:
                state, next_attempt = 'failed', False
            else:
                delay = RETRY_DELAYS_MINUTES[min(attempts, len(RETRY_DELAYS_MINUTES)) - 1]
                state, next_attempt = 'retry', now + timedelta(minutes=delay)
            vals = {
                'state': state,
                'attempts': attempts,
                'next_attempt': next_attempt,
                'error_message': message or '',
            }
            if status:
                status.write(vals)
            else:
                vals.update(model_name=model_name, field_name=field_name, res_id=res_id)
                to_create.append(vals)
        if to_create:
            self.create(to_create)

    @api.model
    def _record_field_error(self, model_name, field_name, message):
        """A configuration error of the field (unknown placeholder, engine...): one
        line with record ID 0, removed as soon as the field can be computed."""
        line = self.search([('model_name', '=', model_name), ('field_name', '=', field_name),
                            ('res_id', '=', FIELD_ERROR_RES_ID)], limit=1)
        vals = {
            'state': 'failed',
            'attempts': line.attempts + 1 if line else 1,
            'next_attempt': False,
            'error_message': 'Field configuration: %s' % message,
        }
        if line:
            line.write(vals)
        else:
            self.create(dict(vals, model_name=model_name, field_name=field_name, res_id=FIELD_ERROR_RES_ID))

    @api.model
    def _clear_field_error(self, model_name, field_name):
        self.search([('model_name', '=', model_name), ('field_name', '=', field_name),
                     ('res_id', '=', FIELD_ERROR_RES_ID)]).unlink()

    def action_retry(self):
        """Compute these records again at the next cron run."""
        for status in self:
            Model = self.env.get(status.model_name)
            pending_name = '%s_pending' % status.field_name
            if Model is None or pending_name not in Model._fields:
                continue
            record = Model.sudo().browse(status.res_id).exists()
            if record:
                record.write({pending_name: True})
        self.unlink()
        return True

    def action_open_record(self):
        self.ensure_one()
        if self.res_id == FIELD_ERROR_RES_ID:
            raise UserError("This line is about the configuration of the field %s.%s, not about a record:\n%s"
                            % (self.model_name, self.field_name, self.error_message))
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.model_name,
            'res_id': self.res_id,
            'view_mode': 'form',
        }
