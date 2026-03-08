# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Abstract mixin that hooks into the registry build process to:

1. Discover all fields with ``ai_compute`` across every loaded model.
2. Create / update ``ai.prompt`` records for each discovered field.
3. Provide the cron method ``_cron_compute_ai_fields`` to process
   pending async os AI fields.
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class AiFieldMixin(models.AbstractModel):
    _name = 'ai.field.mixin'
    _description = 'OS AI Field Mixin'

    def _register_hook(self):
        super()._register_hook()
        _sync_ai_prompts(self.env)

    @api.model
    def _cron_compute_os_ai_fields(self):
        """Cron job: process all pending async AI field computations."""
        from ..patch import (
            _resolve_prompt_template, _get_provider, _compute_batch,
            _detect_required_capability,
        )
        from ..models.res_config_settings import DEFAULT_SYSTEM_PROMPT

        registry = self.env.registry

        # Collect all async ai_compute fields across all models
        ai_async_fields = []
        for model_name, model_cls in registry.items():
            if getattr(model_cls, '_abstract', False):
                continue
            if not hasattr(model_cls, '_fields'):
                continue
            for fname, field in model_cls._fields.items():
                ai_compute = getattr(field, 'ai_compute', None)
                ai_async = getattr(field, 'ai_compute_async', True)
                if ai_compute and ai_async:
                    ai_async_fields.append((model_name, fname, field))

        if not ai_async_fields:
            _logger.debug("os_ai_fields cron: no async ai_compute fields found")
            return

        # Get system prompt once
        system_prompt = self.env['ir.config_parameter'].sudo().get_param(
            'os_ai_fields.system_prompt', DEFAULT_SYSTEM_PROMPT,
        )

        AiLog = self.env.get('os.ai.log')

        for model_name, fname, field in ai_async_fields:
            pending_name = '%s_pending' % fname
            batch_size = getattr(field, 'ai_compute_batch_size', 40)

            # Get the model env
            try:
                Model = self.env[model_name]
            except KeyError:
                continue

            # Check the pending field exists
            if pending_name not in Model._fields:
                _logger.warning(
                    "os_ai_fields cron: pending field %s not found on %s, skipping",
                    pending_name, model_name,
                )
                continue

            # Search for pending records
            pending_records = Model.sudo().search([
                (pending_name, '=', True),
            ])

            if not pending_records:
                continue

            _logger.info(
                "os_ai_fields cron: processing %d pending record(s) for %s.%s",
                len(pending_records), model_name, fname,
            )

            # Resolve prompt template
            prompt_template = _resolve_prompt_template(
                self.env, model_name, fname, field,
            )
            if not prompt_template:
                _logger.warning(
                    "os_ai_fields cron: no prompt for %s.%s, skipping",
                    model_name, fname,
                )
                continue

            # Dynamically determine needed capability per field
            target_field_type = field.type if field else 'char'
            cap = _detect_required_capability(
                self.env, model_name, target_field_type, prompt_template,
                field_obj=field,
            )

            provider = _get_provider(self.env, required_capability=cap)
            if not provider:
                _logger.warning(
                    "os_ai_fields cron: no provider with capability %s for %s.%s, skipping",
                    cap, model_name, fname,
                )
                continue

            # Image capabilities and document fields do not support batching
            actual_batch_size = batch_size
            doc_type = getattr(field, 'ai_document_type', None)
            if cap != 'capable_text' or doc_type:
                actual_batch_size = 1

            # Process in batches, committing after each one so that
            # work already done is not lost if the cron is interrupted.
            record_list = list(pending_records)
            consecutive_errors = 0
            for i in range(0, len(record_list), actual_batch_size):
                batch = record_list[i:i + actual_batch_size]
                try:
                    _compute_batch(
                        self.env, provider, system_prompt, prompt_template,
                        fname, batch, AiLog, cap,
                    )
                    # Mark as no longer pending
                    for record in batch:
                        record[pending_name] = False
                    # Commit so this batch is persisted immediately.
                    # If the cron crashes later, already-processed batches
                    # remain saved and won't be reprocessed.
                    self.env.cr.commit()  # pylint: disable=invalid-commit
                    self.env.clear()      # invalidate caches after commit
                    consecutive_errors = 0
                except Exception as exc:
                    _logger.error(
                        "os_ai_fields cron: error processing batch for %s.%s: %s",
                        model_name, fname, exc,
                    )
                    # Rollback the failed batch so the cursor stays usable
                    self.env.cr.rollback()
                    self.env.clear()      # invalidate caches after rollback
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        _logger.error(
                            "os_ai_fields cron: 3 consecutive errors for %s.%s, "
                            "skipping remaining %d record(s)",
                            model_name, fname,
                            len(record_list) - (i + actual_batch_size),
                        )
                        break

        _logger.info("os_ai_fields cron: completed")


# ---------------------------------------------------------------------------
# Prompt sync (runs once per registry build)
# ---------------------------------------------------------------------------

_ai_prompts_synced = set()


def _sync_ai_prompts(env):
    """Scan all models for fields with ai_compute and sync ai.prompt records."""
    registry = env.registry
    reg_id = id(registry)

    if reg_id in _ai_prompts_synced:
        return
    _ai_prompts_synced.add(reg_id)

    ai_fields_info = []
    for model_name, model_cls in registry.items():
        if hasattr(model_cls, '_fields'):
            for fname, field in model_cls._fields.items():
                ai_compute = getattr(field, 'ai_compute', None)
                if ai_compute:
                    ai_fields_info.append((model_name, fname, field))

    if not ai_fields_info:
        _logger.debug("os_ai_fields: no fields with ai_compute found")
        return

    _logger.info(
        "os_ai_fields: found %d field(s) with ai_compute, syncing prompts",
        len(ai_fields_info),
    )

    AiPrompt = env.get('os.ai.prompt')
    if AiPrompt is None:
        _logger.warning("os_ai_fields: ai.prompt model not available, skipping prompt sync")
        return

    IrModel = env['ir.model']

    for model_name, fname, field in ai_fields_info:
        prompt_text = getattr(field, 'ai_compute', '')
        if not prompt_text:
            continue

        ir_model = IrModel.search([('model', '=', model_name)], limit=1)
        if not ir_model:
            continue

        existing = AiPrompt.search([
            ('model_id', '=', ir_model.id),
            ('field_name', '=', fname),
        ], limit=1)

        vals = {
            'name': "AI: %s.%s" % (model_name, fname),
            'model_id': ir_model.id,
            'field_name': fname,
            'prompt_text': prompt_text,
        }

        if existing:
            existing.write({'prompt_text': prompt_text})
            _logger.debug("os_ai_fields: updated prompt for %s.%s", model_name, fname)
        else:
            AiPrompt.sudo().create(vals)
            _logger.debug("os_ai_fields: created prompt for %s.%s", model_name, fname)

    _logger.info("os_ai_fields: prompt sync complete")
