# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class ResLang(models.Model):
    _inherit = 'res.lang'

    def write(self, vals):
        # Detect language activation (active going from False to True)
        newly_activated = self.env['res.lang']
        if vals.get('active') is True:
            newly_activated = self.filtered(lambda l: not l.active)

        res = super().write(vals)

        if newly_activated:
            self._recompute_ai_translations(newly_activated)

        return res

    def _recompute_ai_translations(self, new_langs):
        """Mark all translatable ai_compute fields as pending for recomputation.

        When a new language is activated, records with translatable AI fields
        need to be recomputed so the LLM generates the value in the new language.
        """
        registry = self.env.registry
        lang_names = ', '.join(new_langs.mapped('code'))

        for model_name in list(registry):
            Model = self.env.get(model_name)
            if Model is None:
                continue
            if getattr(Model, '_abstract', False) or getattr(Model, '_transient', False):
                continue

            for fname, field in Model._fields.items():
                ai_compute = getattr(field, 'ai_compute', None)
                translate = getattr(field, 'translate', False)
                ai_async = getattr(field, 'ai_compute_async', True)
                if not (ai_compute and translate and ai_async):
                    continue

                pending_name = '%s_pending' % fname
                if pending_name not in Model._fields:
                    continue

                try:
                    records = Model.sudo().search([])
                    if records:
                        records.write({pending_name: True})
                        _logger.info(
                            "os_ai_fields: marked %d records pending for %s.%s "
                            "(new language: %s)",
                            len(records), model_name, fname, lang_names,
                        )
                except Exception as exc:
                    _logger.error(
                        "os_ai_fields: failed to mark pending for %s.%s: %s",
                        model_name, fname, exc,
                    )
