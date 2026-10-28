# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class AiLog(models.Model):
    _inherit = 'os.ai.log'

    # `success` only says that the provider answered. These fields say whether
    # the answer could be parsed, validated and stored on each record.
    applied_count = fields.Integer(
        'Records Updated',
        help="Records whose value was validated and stored from this call.",
    )
    failed_count = fields.Integer(
        'Records Not Updated',
        help="Records that kept their previous value (invalid, missing or rejected answer).",
    )
    apply_errors = fields.Text(
        'Application Errors',
        help="Why the answer could not be applied, per record.",
    )
