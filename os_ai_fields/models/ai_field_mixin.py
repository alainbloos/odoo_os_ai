# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Abstract mixin that hooks into the registry build process to:

1. Discover all fields with ``ai_compute`` across every loaded model.
2. Create / update ``ai.prompt`` records for each discovered field.
3. Provide the cron method ``_cron_compute_os_ai_fields`` to process
   pending async os AI fields.
"""

import logging
import random
import time

from odoo import api, models

from ..compat import clear_env, get_param

_logger = logging.getLogger(__name__)

# Pending records read per field and cron run; the rest waits for the next runs
MAX_PENDING_PER_RUN = 10000

# Maximum duration of one cron run (seconds), system parameter
# os_ai_fields.cron_time_budget. Records not reached stay pending.
CRON_TIME_BUDGET_PARAM = 'os_ai_fields.cron_time_budget'
DEFAULT_CRON_TIME_BUDGET = 600

# Stop processing a field after this many consecutive batches without any
# record updated (provider down, invalid configuration...).
MAX_CONSECUTIVE_FAILED_BATCHES = 3


class AiFieldMixin(models.AbstractModel):
    _name = 'ai.field.mixin'
    _description = 'OS AI Field Mixin'

    def _register_hook(self):
        super()._register_hook()
        _sync_ai_prompts(self.env)

    @api.model
    def _cron_compute_os_ai_fields(self):
        """Cron job: process all pending async AI field computations."""
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

        try:
            budget = float(get_param(self.env, CRON_TIME_BUDGET_PARAM, DEFAULT_CRON_TIME_BUDGET))
        except (TypeError, ValueError):
            budget = DEFAULT_CRON_TIME_BUDGET
        deadline = time.monotonic() + budget if budget > 0 else None

        # Start at a different field on each run: with a short time budget and a
        # sustained load, the last fields would otherwise wait forever.
        start = random.randrange(len(ai_async_fields))
        for model_name, fname, field in ai_async_fields[start:] + ai_async_fields[:start]:
            if deadline and time.monotonic() > deadline:
                _logger.info("os_ai_fields cron: time budget of %ss reached, "
                             "remaining fields will be processed at the next run", budget)
                break
            self._process_pending_ai_field(model_name, fname, field, deadline=deadline)

        _logger.info("os_ai_fields cron: completed")

    @api.model
    def _create_ai_logs(self, vals_list):
        """Create the AI Logs collected during a batch (see patch._log_call)."""
        AiLog = self.env.get('os.ai.log')
        if AiLog is None or not vals_list:
            return
        if 'applied_count' not in AiLog._fields:
            for vals in vals_list:
                for key in ('applied_count', 'failed_count', 'apply_errors'):
                    vals.pop(key, None)
        try:
            with self.env.cr.savepoint():
                AiLog.sudo().create(vals_list)
        except Exception as exc:
            _logger.error("os_ai_fields cron: failed to create the AI logs of a batch: %s", exc)

    @api.model
    def _process_pending_ai_field(self, model_name, fname, field, deadline=None):
        """Compute the pending records of one async AI field, batch by batch.

        Only records whose value was really updated leave the pending state.
        Failures are stored in os.ai.compute.status, which also tells which
        records must wait (retry delay) or are blocked (permanent failure).
        Each batch is committed so that work already done is not lost if the
        cron is interrupted.
        """
        from ..patch import (
            ABSTAINED, DONE, NO_PROVIDER_REASON, RETRYABLE, _is_transaction_conflict, compute_batch,
            prepare_computation,
        )

        pending_name = '%s_pending' % fname

        # Get the model env
        try:
            Model = self.env[model_name].sudo()
        except KeyError:
            return

        # Check the pending field exists
        if pending_name not in Model._fields:
            _logger.warning(
                "os_ai_fields cron: pending field %s not found on %s, skipping",
                pending_name, model_name,
            )
            return

        Status = self.env['os.ai.compute.status'].sudo()
        domain = [(pending_name, '=', True)]
        blocked_ids = Status._get_blocked_ids(model_name, fname)
        if blocked_ids:
            domain.append(('id', 'not in', list(blocked_ids)))
        # bounded per run: a large backlog is processed over several runs
        record_ids = Model.search(domain, order='id', limit=MAX_PENDING_PER_RUN).ids
        if not record_ids:
            return

        plan, reason = prepare_computation(
            self.env, model_name, fname, field, getattr(field, 'ai_compute_batch_size', 40),
        )
        if not plan:
            # No provider is a deployment choice; anything else is a configuration
            # error of the field, also shown in AI Computation Errors.
            if reason.startswith(NO_PROVIDER_REASON):
                _logger.warning("os_ai_fields cron: %d pending record(s) for %s.%s not computed: %s",
                                len(record_ids), model_name, fname, reason)
                Status._clear_field_error(model_name, fname)
            else:
                _logger.error("os_ai_fields cron: %d pending record(s) for %s.%s not computed: %s",
                              len(record_ids), model_name, fname, reason)
                Status._record_field_error(model_name, fname, reason)
            self.env.cr.commit()  # pylint: disable=invalid-commit
            return
        Status._clear_field_error(model_name, fname)
        if deadline:
            # calls never wait beyond the time budget
            plan['provider'] = plan['provider'].with_context(os_ai_deadline=deadline)

        _logger.info(
            "os_ai_fields cron: processing %d pending record(s) for %s.%s",
            len(record_ids), model_name, fname,
        )

        batch_size = plan['batch_size']
        failed_batches = 0
        for i in range(0, len(record_ids), batch_size):
            if deadline and time.monotonic() > deadline:
                _logger.info("os_ai_fields cron: time budget reached, %d record(s) of %s.%s left pending",
                             len(record_ids) - i, model_name, fname)
                break
            batch = Model.browse(record_ids[i:i + batch_size]).exists()
            if not batch:
                continue
            batch_ids = batch.ids
            plan['log_sink'] = logs = []
            try:
                results = compute_batch(self.env, plan, list(batch))
                done_ids = [rid for rid, (outcome, _msg) in results.items() if outcome == DONE]
                # An abstention (decision model not confident) keeps the value
                # and is not retried: it is recorded instead.
                settled_ids = [rid for rid, (outcome, _msg) in results.items() if outcome in (DONE, ABSTAINED)]
                failures = {rid: res for rid, res in results.items() if res[0] != DONE}
                if settled_ids:
                    Model.browse(settled_ids).write({pending_name: False})
                if done_ids:
                    Status._clear(model_name, fname, done_ids)
                Status._record_failures(model_name, fname, failures)
                self._create_ai_logs(logs)
                self.env.cr.commit()  # pylint: disable=invalid-commit
            except Exception as exc:
                # Undo the partial batch; the records stay pending.
                self.env.cr.rollback()
                clear_env(self.env)
                done_ids = settled_ids = []
                # The provider answered (and may have billed) the calls: log them,
                # as not applied.
                for vals in logs:
                    vals.update(applied_count=0, failed_count=len(batch_ids),
                                apply_errors='Rolled back, nothing stored: %s' % exc)
                if _is_transaction_conflict(exc):
                    # Records changed while the LLM was answering: the answer is
                    # stale, compute them again at the next run.
                    _logger.warning(
                        "os_ai_fields cron: %s.%s records %s changed during the computation, "
                        "they will be computed again: %s", model_name, fname, batch_ids, exc,
                    )
                    self._create_ai_logs(logs)
                    self.env.cr.commit()  # pylint: disable=invalid-commit
                else:
                    _logger.exception(
                        "os_ai_fields cron: error processing batch for %s.%s: %s",
                        model_name, fname, exc,
                    )
                    # Remember the failure in a clean transaction so that the
                    # records are retried later, with a delay.
                    Status._record_failures(model_name, fname, {
                        rid: (RETRYABLE, 'Unexpected error: %s' % exc) for rid in batch_ids
                    })
                    self._create_ai_logs(logs)
                    self.env.cr.commit()  # pylint: disable=invalid-commit
            clear_env(self.env)  # invalidate caches after commit

            failed_batches = 0 if (done_ids or settled_ids) else failed_batches + 1
            if failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                _logger.error(
                    "os_ai_fields cron: %d consecutive batches without any update for %s.%s, "
                    "skipping remaining %d record(s) until the next run",
                    failed_batches, model_name, fname, len(record_ids) - (i + batch_size),
                )
                break


# ---------------------------------------------------------------------------
# Prompt sync (runs once per registry build)
# ---------------------------------------------------------------------------

_ai_prompts_synced = set()


def _sync_ai_prompts(env):
    """Scan all models for fields with ai_compute and sync ai.prompt records.

    The prompt defined in the code is stored as ``default_prompt_text``.
    ``prompt_text`` follows it only while the user did not customize it.
    """
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

    from ..patch import _uncovered_placeholders
    for model_name, fname, field in ai_fields_info:
        uncovered = _uncovered_placeholders(env, model_name, getattr(field, 'ai_compute', ''),
                                            getattr(field, 'ai_compute_depends', None))
        if uncovered:
            _logger.warning(
                "os_ai_fields: %s.%s reads %s in its prompt but ai_compute_depends does not list it: "
                "changes there do not compute the field again", model_name, fname, ', '.join(uncovered))

    _logger.info(
        "os_ai_fields: found %d field(s) with ai_compute, syncing prompts",
        len(ai_fields_info),
    )

    AiPrompt = env.get('os.ai.prompt')
    if AiPrompt is None:
        _logger.warning("os_ai_fields: ai.prompt model not available, skipping prompt sync")
        return
    AiPrompt = AiPrompt.sudo().with_context(active_test=False)

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

        if not existing:
            AiPrompt.create({
                'name': "AI: %s.%s" % (model_name, fname),
                'model_id': ir_model.id,
                'field_name': fname,
                'prompt_text': prompt_text,
                'default_prompt_text': prompt_text,
            })
            _logger.debug("os_ai_fields: created prompt for %s.%s", model_name, fname)
        elif existing.default_prompt_text != prompt_text:
            vals = {'default_prompt_text': prompt_text}
            # Follow the code only while the prompt is not customized. Prompts
            # created before default_prompt_text existed are kept as they are
            # unless they already match the code.
            if existing.prompt_text == (existing.default_prompt_text or prompt_text):
                vals['prompt_text'] = prompt_text
            existing.write(vals)
            _logger.debug("os_ai_fields: updated default prompt for %s.%s (customized=%s)",
                          model_name, fname, 'prompt_text' not in vals)

    _logger.info("os_ai_fields: prompt sync complete")
