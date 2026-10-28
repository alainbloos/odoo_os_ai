# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Non-invasive monkey-patch of odoo.fields.Field to support ai_compute,
ai_compute_depends, ai_compute_async and ai_compute_batch_size parameters.

Key insight: we must set `compute` and `depends` on the field BEFORE Odoo's
field setup runs (in `_get_attrs`), so that the registry properly builds
field_depends and _field_triggers.

When ai_compute_async=True (default), the compute method only marks a companion
Boolean `{field}_pending` = True and returns immediately.  A cron job later
processes all pending records.

Every LLM answer is parsed and validated before it touches a record: a failed
call, an invalid or missing answer, or a value the database rejects leaves the
previous value in place.  `_compute_batch` reports the outcome per record
(DONE, RETRYABLE or PERMANENT) so that the cron only finishes the records that
were really updated.
"""

import datetime
import json
import logging
import math
import re
from collections import namedtuple

from odoo import fields as odoo_fields
from odoo import models as odoo_models
from odoo import api
from odoo import release as odoo_release
from odoo.tools.safe_eval import safe_eval

from odoo.addons.os_ai.models.ai_provider import ERROR_PERMANENT, ERROR_RETRYABLE

from .compat import get_param

_logger = logging.getLogger(__name__)

# Outcome of an AI computation, per record
DONE = 'done'
RETRYABLE = ERROR_RETRYABLE
PERMANENT = ERROR_PERMANENT
ABSTAINED = 'abstained'  # decision engine not confident enough: value kept

# Decision engine (ai_compute_engine='decision', e.g. TypeSafe Jev)
DECISION_ENGINE = 'decision'
DECISION_FIELD_TYPES = ('boolean', 'selection', 'many2one')
DECISION_MAX_OPTIONS = 255      # per Choice question, including the "none" option
DECISION_NONE = '__none__'
_PLACEHOLDER_RE = re.compile(r'\{([^{}]+)\}')
# {field} or {relation.field}; other text between braces (e.g. JSON) is literal
_FIELD_PATH_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')
SYSTEM_PLACEHOLDERS = frozenset({'__date__', '__user__', '__company__'})
NO_PROVIDER_REASON = 'no active provider'

# Maximum number of relational candidates offered to the LLM (per field:
# ai_compute_options_limit).  Above it the field is not computed.
DEFAULT_OPTIONS_LIMIT = 200

TEXT_FIELD_TYPES = ('char', 'text', 'html')
RELATIONAL_FIELD_TYPES = ('many2one', 'many2many')
IMAGE_CAPABILITIES = ('capable_image_generation', 'capable_image_edit')

TRUE_WORDS = frozenset({'true', 'yes', 'y', 't', '1', 'si', 'sí', 'verdadero'})
FALSE_WORDS = frozenset({'false', 'no', 'n', 'f', '0', 'falso'})
NONE_WORDS = frozenset({'', 'none', 'null', 'false', '0'})

_NUMBER_RE = re.compile(r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?')

# Integer fields are stored as PostgreSQL int4
INT32_MIN, INT32_MAX = -2 ** 31, 2 ** 31 - 1

# ---------------------------------------------------------------------------
# 1) Add new class-level attributes to Field
# ---------------------------------------------------------------------------
odoo_fields.Field.ai_compute = None           # prompt template string
odoo_fields.Field.ai_compute_depends = None    # list of dependency field names
odoo_fields.Field.ai_compute_async = True      # True = cron-based (default), False = blocking
odoo_fields.Field.ai_compute_batch_size = 40   # max records per LLM call
odoo_fields.Field.ai_compute_options_limit = None  # max relational candidates (default 200)
odoo_fields.Field.ai_document_type = None      # 'pdf', 'xlsx' — for Binary fields that generate documents
odoo_fields.Field.ai_compute_engine = 'llm'    # 'llm' or 'decision' (typed decision model, opt-in)
odoo_fields.Field.ai_decision_threshold = 0.5  # Boolean: probability of "yes" above which the answer is True
odoo_fields.Field.ai_decision_margin = 0.15    # Boolean: uncertain zone around the threshold (value kept)
odoo_fields.Field.ai_decision_min_confidence = 0.5  # Selection/Many2one: minimum confidence to store the choice

# ---------------------------------------------------------------------------
# 2) Make _valid_field_parameter accept the new keys
# ---------------------------------------------------------------------------
_AI_FIELD_PARAMS = frozenset({
    'ai_compute', 'ai_compute_depends', 'ai_compute_async',
    'ai_compute_batch_size', 'ai_compute_options_limit', 'ai_document_type',
    'ai_compute_engine', 'ai_decision_threshold', 'ai_decision_margin', 'ai_decision_min_confidence',
})

_original_valid_field_parameter = odoo_models.BaseModel._valid_field_parameter

def _patched_valid_field_parameter(self, field, name):
    if name in _AI_FIELD_PARAMS:
        return True
    return _original_valid_field_parameter(self, field, name)

odoo_models.BaseModel._valid_field_parameter = _patched_valid_field_parameter

# ---------------------------------------------------------------------------
# 3) Patch _get_attrs to inject `compute` for ai_compute fields
#    AND auto-create _pending companion fields for ai_compute_async=True
# ---------------------------------------------------------------------------
_original_get_attrs = odoo_fields.Field._get_attrs


def _patched_get_attrs(self, model_class, name):
    attrs = _original_get_attrs(self, model_class, name)

    ai_compute_prompt = attrs.get('ai_compute')
    if ai_compute_prompt:
        ai_depends = attrs.get('ai_compute_depends') or []
        ai_batch_size = attrs.get('ai_compute_batch_size', 40)
        store = attrs.get('store', True)
        # A non-stored field cannot wait for the cron: compute it synchronously.
        ai_async = attrs.get('ai_compute_async', True) and store
        attrs['ai_compute_async'] = ai_async

        compute_name = '_compute_ai_%s' % name
        model_name = model_class._name

        # A new function per class: the registry may build several classes
        # (databases, reloads) for the same model and field.
        compute_method = _create_ai_compute_method(compute_name, name, ai_async, ai_batch_size)
        if ai_depends:
            compute_method = api.depends(*ai_depends)(compute_method)

        setattr(model_class, compute_name, compute_method)

        attrs['compute'] = compute_name
        attrs['store'] = store
        attrs['readonly'] = attrs.get('readonly', True)
        attrs['compute_sudo'] = attrs.get('compute_sudo', True)
        if not (attrs['store'] and not attrs.get('readonly', True)):
            attrs['copy'] = attrs.get('copy', False)

        # -- Auto-inject _pending companion field for async fields --
        if ai_async:
            pending_name = '%s_pending' % name
            if not hasattr(model_class, pending_name):
                pending_field = odoo_fields.Boolean(
                    string='%s Pending' % name.replace('_', ' ').title(),
                    default=False,
                    store=True,
                    readonly=True,
                    copy=False,
                )
                setattr(model_class, pending_name, pending_field)
                # Also trigger __set_name__ so Odoo knows about the field
                pending_field.__set_name__(model_class, pending_name)
                _logger.debug(
                    "os_ai_fields: injected companion field %s.%s",
                    model_name, pending_name,
                )

        _logger.debug(
            "os_ai_fields: injected compute=%s on %s.%s (store=%s, depends=%s, async=%s, batch=%d)",
            compute_name, model_name, name, attrs.get('store'), ai_depends, ai_async, ai_batch_size,
        )

    return attrs

odoo_fields.Field._get_attrs = _patched_get_attrs

def _uncovered_placeholders(env, model_name, prompt, depends):
    """Placeholders whose changes do not trigger the field.

    A dotted placeholder ({parent_id.name}) must be listed as such; a relational
    one ({category_id}) shows display names, so the field must depend on a
    path through it ('category_id.name'), not only on the relation.
    """
    depends = set(depends or ())
    uncovered = set()
    for token in {token.strip() for token in _PLACEHOLDER_RE.findall(prompt or '')}:
        if token in SYSTEM_PLACEHOLDERS or not _FIELD_PATH_RE.fullmatch(token):
            continue
        if '.' in token:
            if token not in depends:
                uncovered.add(token)
            continue
        field = env[model_name]._fields.get(token)
        if field is not None and field.relational and field.type != 'many2one_reference' \
                and not any(dep.startswith(token + '.') for dep in depends):
            uncovered.add('%s (display names)' % token)
    return sorted(uncovered)


# ---------------------------------------------------------------------------
# 4) Compute method factory
# ---------------------------------------------------------------------------

def _create_ai_compute_method(compute_name, field_name, is_async, batch_size):
    """Create a compute method for an AI field.

    If is_async=True, the method only flags the records as pending.
    The actual LLM call is done by the cron.
    """

    def _compute_ai(self):
        # Onchange / new records (NewId): nothing to compute yet, keep the
        # value of the record being edited (or none for a new one).
        new_records = self.filtered(lambda rec: not (isinstance(rec.id, int) and rec.id))
        for record in new_records:
            record[field_name] = record._origin[field_name] if record._origin else False
        records = self - new_records
        if not records:
            return

        # -- ASYNC: just mark as pending and return --
        if is_async:
            _logger.debug("os_ai_fields: marking %d record(s) as pending for %s.%s",
                          len(records), self._name, field_name)
            mark_pending(records, field_name)
            return

        # -- SYNC: compute immediately --
        _compute_sync(records, field_name, batch_size)

    _compute_ai.__name__ = compute_name
    return _compute_ai


def mark_pending(records, field_name):
    """Flag records so that the cron (re)computes ``field_name``.

    Uses the ORM low-level field write: the flag is stored at the next flush
    without calling write() overrides, tracking or other recomputations, which
    is safe from inside a compute method (also during module installation).
    Previous failures of these records are forgotten: their inputs changed.
    """
    pending_field = records._fields.get('%s_pending' % field_name)
    if pending_field is None or not records:
        return
    pending_field.write(records, True)
    # The ORM does not write a flag that is already True. Update the rows anyway:
    # a cron computing them meanwhile from the previous inputs (e.g. before a
    # child was added) then gets a serialization conflict instead of storing a
    # stale answer and clearing the flag.
    ids = tuple(rid for rid in records.ids if isinstance(rid, int))
    if ids and pending_field.store:
        records.env.cr.execute(
            'UPDATE "%s" SET "%s" = TRUE WHERE id IN %%s' % (records._table, pending_field.name), [ids])
    if 'os.ai.compute.status' in records.env.registry:
        records.env['os.ai.compute.status'].sudo()._clear(records._name, field_name, records.ids)


def _compute_sync(records, field_name, batch_size):
    """Compute a blocking AI field inside its compute method.

    Records that cannot be computed keep their stored value.
    """
    env = records.env
    field_obj = records._fields[field_name]
    plan, reason = prepare_computation(env, records._name, field_name, field_obj, batch_size)
    results = {}
    if plan:
        _logger.debug("os_ai_fields: computing %s.%s for %d record(s) (batch_size=%d)",
                      records._name, field_name, len(records), plan['batch_size'])
        record_list = list(records)
        for i in range(0, len(record_list), plan['batch_size']):
            batch = record_list[i:i + plan['batch_size']]
            results.update(compute_batch(env, plan, batch, use_savepoint=False))
    else:
        _logger.warning("os_ai_fields: cannot compute %s.%s: %s", records._name, field_name, reason)

    if not field_obj.store:
        # A non-stored computed field must always receive a value
        for record in records:
            if results.get(record.id, (None,))[0] != DONE:
                record[field_name] = False


# ---------------------------------------------------------------------------
# 5) Shared helpers for both sync compute and cron
# ---------------------------------------------------------------------------

def _resolve_prompt_template(env, model_name, field_name, field_obj):
    """Find the prompt template for a field, from ai.prompt or the field definition."""
    AiPrompt = env.get('os.ai.prompt')
    prompt_template = None

    if AiPrompt is not None:
        IrModel = env['ir.model']
        ir_model = IrModel.search([('model', '=', model_name)], limit=1)
        if ir_model:
            prompt_rec = AiPrompt.search([
                ('model_id', '=', ir_model.id),
                ('field_name', '=', field_name),
                ('active', '=', True),
            ], order='sequence, id', limit=1)
            if prompt_rec:
                prompt_template = prompt_rec.prompt_text

    if not prompt_template and field_obj:
        prompt_template = getattr(field_obj, 'ai_compute', None)

    return prompt_template


def _detect_required_capability(env, model_name, target_field_type, prompt_template, field_obj=None):
    """Determine the required AI capability based on target field and prompt tokens.

    Rules:
      Target=binary + ai_document_type  = capable_text  (LLM generates text, local post-processing)
      Target=binary + Has input images  = capable_image_edit
      Target=binary + No input images   = capable_image_generation
      Target=text   + Has input images  = capable_vision
      Target=text   + No input images   = capable_text

    Note: Odoo's fields.Image has type='binary', so we check for 'binary' only.
    """
    if not prompt_template:
        return 'capable_text'

    has_image_input = False
    tokens = re.findall(r'\{([^{}]+)\}', prompt_template)

    for token in tokens:
        token = token.strip()
        if token.startswith('__'):
            continue

        parts = token.split('.')
        current_model = model_name
        is_binary = False

        for part in parts:
            Model = env.get(current_model)
            if Model is None:
                break
            dep_field = Model._fields.get(part)
            if not dep_field:
                break

            if dep_field.type == 'binary':
                is_binary = True
                break
            elif dep_field.type in ('many2one', 'one2many', 'many2many'):
                current_model = dep_field.comodel_name

        if is_binary:
            has_image_input = True
            break

    if target_field_type == 'binary':
        # Document fields use capable_text — the LLM generates structured text,
        # local post-processing converts it to the final binary format.
        doc_type = getattr(field_obj, 'ai_document_type', None) if field_obj else None
        if doc_type:
            return 'capable_text'
        return 'capable_image_edit' if has_image_input else 'capable_image_generation'
    else:
        return 'capable_vision' if has_image_input else 'capable_text'


def _get_provider(env, required_capability='capable_text'):
    """Find the active provider with the lowest sequence matching the given capability."""
    AiProvider = env.get('os.ai.provider')
    if AiProvider is None:
        _logger.warning("os_ai_fields: ai.provider model not available")
        return None

    domain = [('active', '=', True)]
    if required_capability:
        domain.append((required_capability, '=', True))

    provider = AiProvider.search(domain, order='sequence, id', limit=1)

    if not provider:
        _logger.warning("os_ai_fields: no active ai.provider found with capability %s", required_capability)
        return None

    return provider


def _check_placeholders(env, model_name, template):
    """Error message for a placeholder that is not a field path of the model, else None."""
    for token in sorted({token.strip() for token in _PLACEHOLDER_RE.findall(template or '')}):
        if token in SYSTEM_PLACEHOLDERS or not _FIELD_PATH_RE.fullmatch(token):
            continue
        Model = env[model_name]
        parts = token.split('.')
        for index, part in enumerate(parts):
            field = Model._fields.get(part)
            if field is None:
                return 'invalid placeholder {%s}: %s has no field %s' % (token, Model._name, part)
            if index < len(parts) - 1:
                if not field.relational:
                    return 'invalid placeholder {%s}: %s is not a relational field' % (token, part)
                Model = env[field.comodel_name]
    return None


def prepare_computation(env, model_name, field_name, field_obj, batch_size):
    """Collect everything needed to compute a field.

    Returns:
        tuple: (plan dict or None, reason when None)
    """
    from .models.res_config_settings import DEFAULT_SYSTEM_PROMPT, PARAM_KEY

    prompt_template = _resolve_prompt_template(env, model_name, field_name, field_obj)
    if not prompt_template:
        return None, 'no prompt template'
    if getattr(field_obj, 'ai_compute_async', True) and not getattr(field_obj, 'compute_sudo', True):
        return None, ('compute_sudo=False needs ai_compute_async=False: the cron computes as '
                      'superuser and has no user whose access rules could apply')

    # Dynamically determine needed capability based on target field and prompt tokens
    placeholder_error = _check_placeholders(env, model_name, prompt_template)
    if placeholder_error:
        return None, placeholder_error

    cap = _detect_required_capability(env, model_name, field_obj.type, prompt_template, field_obj=field_obj)
    engine = getattr(field_obj, 'ai_compute_engine', None) or 'llm'
    if engine not in ('llm', DECISION_ENGINE):
        return None, "unknown ai_compute_engine %r (expected 'llm' or 'decision')" % engine
    if engine == DECISION_ENGINE:
        if field_obj.type not in DECISION_FIELD_TYPES:
            return None, 'the decision engine supports Boolean, Selection and Many2one fields'
        if cap != 'capable_text':
            return None, 'the decision engine only reads text'
        parameter_error = _check_decision_parameters(field_obj)
        if parameter_error:
            return None, 'invalid decision parameters: %s' % parameter_error
        cap = 'capable_decision'
        batch_size = 1  # one state per request
    provider = _get_provider(env, required_capability=cap)
    if not provider:
        return None, '%s with capability %s' % (NO_PROVIDER_REASON, cap)

    # Images and documents are generated one by one; relational fields whose
    # candidates depend on the record are asked one record at a time.
    if cap != 'capable_text' or getattr(field_obj, 'ai_document_type', None):
        batch_size = 1
    elif field_obj.type in RELATIONAL_FIELD_TYPES and (
            not _has_static_domain(field_obj) or _candidates_depend_on_company(env, field_obj)):
        batch_size = 1

    return {
        'engine': engine,
        'field_name': field_name,
        'field_obj': field_obj,
        'prompt_template': prompt_template,
        'cap': cap,
        'provider': provider,
        'system_prompt': get_param(env, PARAM_KEY, DEFAULT_SYSTEM_PROMPT),
        'batch_size': max(1, batch_size or 1),
    }, None


def _postprocess_document(env, doc_type, content_str):
    """Convert LLM-generated text content into a binary document.

    Args:
        env: Odoo Environment.
        doc_type: 'xlsx' or 'pdf'.
        content_str: Raw LLM response string.

    Returns:
        str: base64-encoded binary content, or False on error.
    """
    from . import document_utils

    if doc_type == 'xlsx':
        return document_utils.json_to_xlsx(content_str)
    elif doc_type == 'pdf':
        # HTML, or the HTML in a {"value": "<html>..."} wrapper. Any other JSON
        # is not a document: it must not replace the previous one.
        js = _loads_json(content_str)
        if js is _NOT_JSON:
            if _looks_like_json(content_str):
                raise ValueError('malformed JSON answer')
            html_str = content_str
        elif isinstance(js, str):
            html_str = js
        elif isinstance(js, dict) and isinstance(js.get('value'), str):
            html_str = js['value']
        else:
            raise ValueError('expected HTML or {"value": "<html>"}')
        return document_utils.html_to_pdf(env, html_str)
    else:
        _logger.error("os_ai_fields: unknown document type '%s'", doc_type)
        return False


def compute_batch(env, plan, batch, use_savepoint=True):
    """Compute a batch of records in a single LLM call.

    Args:
        plan (dict): from prepare_computation().
        batch (list): records of the same model.
        use_savepoint (bool): store each value in its own savepoint so that a
            value rejected by the database does not affect the other records.
            Must be False when called from a compute method.

    Returns:
        dict: {record_id: (outcome, message)} with outcome DONE, RETRYABLE or
        PERMANENT. Records not DONE keep their previous value.
    """
    from .helpers import resolve_prompt

    if plan.get('engine') == DECISION_ENGINE:
        return _compute_decisions(env, plan, batch, use_savepoint)

    field_name = plan['field_name']
    field_obj = plan['field_obj']
    field_type = field_obj.type
    cap = plan['cap']
    provider = plan['provider']
    system_prompt = plan['system_prompt']
    model_name = batch[0]._name
    record_ids = [r.id for r in batch]

    # Detect translatable fields
    is_translatable = bool(getattr(field_obj, 'translate', False))
    translation_suffix = ''
    lang_codes = []
    if is_translatable:
        translation_suffix, lang_codes = _build_translation_suffix(env)
    multi_lang = is_translatable and len(lang_codes) > 1

    # Detect document type (pdf, xlsx)
    doc_type = getattr(field_obj, 'ai_document_type', None)

    # For relational fields, fetch the candidates the LLM may choose from.
    # Records whose candidates depend on the record itself come one by one.
    candidates = None
    options_suffix = ''
    if field_type in RELATIONAL_FIELD_TYPES:
        candidates = get_relational_candidates(env, field_obj, batch[0])
        if candidates.status != 'ok':
            outcome = RETRYABLE if candidates.status == 'empty' else PERMANENT
            _logger.warning("os_ai_fields: %s.%s not computed: %s", model_name, field_name, candidates.message)
            return {rid: (outcome, candidates.message) for rid in record_ids}
        options_suffix = candidates.suffix
    elif field_type == 'selection':
        options_suffix = _build_selection_options(env, field_obj)
    candidate_ids = candidates.ids if candidates else None

    value_schema = _value_schema(env, field_obj, candidate_ids)
    schema = _build_json_schema(value_schema, len(batch) > 1, is_translatable, lang_codes, doc_type=doc_type)

    # Add document format instructions
    doc_suffix = ''
    if doc_type == 'xlsx':
        doc_suffix = (
            'Return ONLY a valid JSON object with "headers" (array of column names as strings) '
            'and "rows" (array of arrays with ALL values as strings, including numbers). '
            'Example: {"headers": ["Name", "Value"], "rows": [["A", "1"], ["B", "2"]]}'
        )
    elif doc_type == 'pdf':
        doc_suffix = (
            'Return ONLY a valid HTML document for rendering as PDF. '
            'Use clean, well-structured HTML with tables, headings, lists, etc. '
            'Do not use external CSS or JavaScript. Keep inline styles minimal.'
        )

    # Note: For Vision and Image capabilities, batch length will be 1 as enforced upstream
    if len(batch) == 1:
        record = batch[0]
        try:
            user_prompt, images_list = resolve_prompt(plan['prompt_template'], record)
        except ValueError as exc:
            _logger.error("os_ai_fields: %s.%s id=%s: %s", model_name, field_name, record.id, exc)
            return {record.id: (PERMANENT, 'Prompt: %s' % exc)}
        if options_suffix:
            user_prompt = user_prompt + '\n\n' + options_suffix
        if translation_suffix:
            user_prompt = user_prompt + '\n\n' + translation_suffix
        if doc_suffix:
            user_prompt = user_prompt + '\n\n' + doc_suffix

        _logger.debug("os_ai_fields: calling LLM for %s.%s (id=%s) [single schema], cap=%s",
                      model_name, field_name, record.id, cap)
        result = provider.sudo().call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format=schema if cap in ('capable_text', 'capable_vision') else None,
            images=images_list,
            capability_used=cap,
        )
        log_vals = _log_call(env, plan, model_name, field_name, [record.id], system_prompt, user_prompt, result, 1)

        if result['success']:
            try:
                value = _extract_single_value(
                    record, field_obj, result['content'], cap, doc_type,
                    multi_lang, lang_codes, candidate_ids,
                )
            except ValueError as exc:
                results = {record.id: (RETRYABLE, 'Invalid answer: %s' % exc)}
            else:
                results = {record.id: _store_value(env, record, field_name, value, multi_lang, use_savepoint)}
        else:
            results = {record.id: _call_failure(result)}
    else:
        record_prompts = {}
        prompt_errors = {}
        for record in batch:
            try:
                resolved, _images = resolve_prompt(plan['prompt_template'], record)
            except ValueError as exc:
                _logger.error("os_ai_fields: %s.%s id=%s: %s", model_name, field_name, record.id, exc)
                prompt_errors[record.id] = (PERMANENT, 'Prompt: %s' % exc)
                continue
            record_prompts[str(record.id)] = resolved
        if prompt_errors:
            batch = [record for record in batch if record.id not in prompt_errors]
            record_ids = [record.id for record in batch]
            if not batch:
                return prompt_errors

        user_prompt = _build_batch_prompt(record_prompts)
        if options_suffix:
            user_prompt = user_prompt + '\n\n' + options_suffix

        # For batch + translatable: use a special suffix that instructs
        # per-record per-language nesting
        if multi_lang:
            user_prompt = user_prompt + '\n\n' + _build_batch_translation_suffix(lang_codes, record_prompts)

        _logger.debug("os_ai_fields: calling LLM for %s.%s batch of %d records [batch schema], cap=%s",
                      model_name, field_name, len(batch), cap)
        result = provider.sudo().call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format=schema,
            images=None,  # Batches are text only due to upstream restriction
            capability_used=cap,
        )
        log_vals = _log_call(env, plan, model_name, field_name, record_ids, system_prompt, user_prompt,
                             result, len(batch))

        if result['success']:
            try:
                answers = _parse_batch_answers(result['content'], record_ids, multi_lang, lang_codes)
            except ValueError as exc:
                results = {rid: (RETRYABLE, 'Invalid answer: %s' % exc) for rid in record_ids}
            else:
                results = {}
                for record in batch:
                    answer = answers.get(str(record.id), _MISSING)
                    if answer is _MISSING:
                        results[record.id] = (RETRYABLE, 'The answer has no value for this record')
                        continue
                    if answer is _DUPLICATE:
                        results[record.id] = (RETRYABLE, 'The answer has several values for this record')
                        continue
                    try:
                        if multi_lang:
                            value = _convert_translations(record, field_obj, answer, lang_codes)
                        else:
                            value = _convert_value(record, field_obj, answer, candidate_ids)
                    except ValueError as exc:
                        results[record.id] = (RETRYABLE, 'Invalid answer: %s' % exc)
                        continue
                    results[record.id] = _store_value(env, record, field_name, value, multi_lang, use_savepoint)
        else:
            results = {rid: _call_failure(result) for rid in record_ids}
        results.update(prompt_errors)  # not sent to the model

    for rid, (outcome, message) in results.items():
        if outcome != DONE:
            _logger.warning("os_ai_fields: %s.%s id=%s not updated (%s): %s",
                            model_name, field_name, rid, outcome, message)

    _log_outcomes(env, plan, log_vals, {rid: res for rid, res in results.items() if rid in record_ids})
    return results


def _call_failure(result):
    return (result.get('error_kind') or RETRYABLE, result.get('error') or 'LLM call failed')


def _store_value(env, record, field_name, value, multi_lang, use_savepoint):
    """Store a validated value. Returns the record outcome."""
    try:
        if use_savepoint:
            # flushes on exit, so that a database error is raised here and
            # only this record is rolled back
            with env.cr.savepoint():
                _assign(record, field_name, value, multi_lang)
        else:
            _assign(record, field_name, value, multi_lang)
    except Exception as exc:
        if _is_transaction_conflict(exc):
            # The record changed while the LLM was answering: the answer may be
            # stale and the transaction must be retried as a whole.
            raise
        _logger.error("os_ai_fields: failed to store %s.%s id=%s: %s",
                      record._name, field_name, record.id, exc)
        return (PERMANENT, 'The value was rejected: %s' % exc)
    return (DONE, None)


def _is_transaction_conflict(exc):
    """Concurrent update or deadlock detected by PostgreSQL."""
    try:
        from psycopg2.extensions import TransactionRollbackError
    except ImportError:
        return False
    return isinstance(exc, TransactionRollbackError)


def _assign(record, field_name, value, multi_lang):
    field_type = record._fields[field_name].type
    if multi_lang:
        for lang_code, lang_value in value.items():
            record.with_context(lang=lang_code)[field_name] = lang_value
    elif field_type == 'many2many':
        record[field_name] = [(6, 0, value)]
    else:
        record[field_name] = value


# ---------------------------------------------------------------------------
# 6) Answer parsing and validation
# ---------------------------------------------------------------------------

_MISSING = object()
_DUPLICATE = object()
_NOT_JSON = object()


_FENCE_RE = re.compile(r'^```[\w-]*[ \t]*\n?(.*?)\n?[ \t]*```$', re.DOTALL)
_OPENING_FENCE_RE = re.compile(r'^```[\w-]*')


class _DuplicateKey(ValueError):
    pass


def _normalize_answer(content):
    """The answer without surrounding blanks and markdown code fence."""
    text = (content or '').strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith('```'):  # opening fence without its end: truncated answer
        return _OPENING_FENCE_RE.sub('', text, count=1).strip()
    return text


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey('the answer repeats the key %r' % key)
        result[key] = value
    return result


def _loads_json(content):
    """Parse a JSON answer, tolerating a markdown code fence. Returns _NOT_JSON when it is not JSON.

    Raises:
        ValueError: a JSON object repeats a key (json.loads would keep the last value).
    """
    try:
        return json.loads(_normalize_answer(content), object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey:
        raise
    except (TypeError, ValueError):
        return _NOT_JSON


def _looks_like_json(content):
    """True for an answer meant to be JSON (e.g. truncated) that could not be parsed."""
    return _normalize_answer(content).startswith(('{', '['))


def _extract_single_value(record, field_obj, content, cap, doc_type, multi_lang, lang_codes, candidate_ids):
    """Turn a single-record answer into a value ready to store. Raises ValueError."""
    if cap in IMAGE_CAPABILITIES:
        if not content:
            raise ValueError('no image in the answer')
        return content

    if doc_type:
        binary_value = _postprocess_document(record.env, doc_type, content)
        if not binary_value:
            raise ValueError('document post-processing failed (%s)' % doc_type)
        return binary_value

    content = (content or '').strip()
    if not content:
        raise ValueError('empty answer')

    parsed = _loads_json(content)
    if parsed is _NOT_JSON and _looks_like_json(content):
        # Looks like JSON but cannot be parsed: truncated or malformed answer
        raise ValueError('malformed JSON answer')
    if multi_lang:
        return _convert_translations(record, field_obj, _translations_from(parsed), lang_codes)

    if isinstance(parsed, dict):
        if 'value' not in parsed:
            raise ValueError('JSON answer without "value"')
        raw = parsed['value']
    elif parsed is _NOT_JSON or isinstance(parsed, (str, int, float)):
        # The model ignored the schema; plain answers are validated below
        raw = _normalize_answer(content) if parsed is _NOT_JSON else parsed
    elif isinstance(parsed, list) and field_obj.type == 'many2many':
        raw = parsed  # the list of IDs itself, validated against the candidates
    else:
        raise ValueError('unexpected JSON answer')
    return _convert_value(record, field_obj, raw, candidate_ids)


def _translations_from(parsed):
    """Extract {lang_code: value} from a translation answer."""
    if not isinstance(parsed, dict):
        raise ValueError('translation answer is not a JSON object')
    if isinstance(parsed.get('translations'), list):
        return _translation_list(parsed['translations'])
    return parsed


def _translation_list(items):
    """{lang_code: value} from [{"lang_code": ..., "value": ...}]; a repeated language is refused."""
    values = {}
    for item in items:
        if isinstance(item, dict) and 'lang_code' in item and 'value' in item:
            if item['lang_code'] in values:
                raise ValueError('the answer repeats the language %s' % item['lang_code'])
            values[item['lang_code']] = item['value']
    return values


def _convert_translations(record, field_obj, translations, lang_codes):
    """Validate a value per installed language; all languages are required."""
    if not isinstance(translations, dict):
        raise ValueError('translations are not a JSON object')
    values = {}
    for lang_code in lang_codes:
        value = translations.get(lang_code)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError('missing translation for %s' % lang_code)
        values[lang_code] = _convert_value(record, field_obj, value)
    return values


def _parse_batch_answers(content, record_ids, multi_lang, lang_codes):
    """Map each record ID (as string) to its raw answer.

    Supports the schema format {"results": [{"id": ..., "value"/"translations": ...}]}
    and the fallback formats {"<id>": value} and, for translations,
    {"<id>": {"<lang>": value}} or {"<lang>": {"<id>": value}}.
    IDs answered more than once map to _DUPLICATE.

    Raises:
        ValueError: the answer is not usable at all.
    """
    parsed = _loads_json(content)
    if not isinstance(parsed, dict):
        raise ValueError('the answer is not a JSON object')

    answers = {}
    duplicates = set()

    def put(rid, value):
        rid = str(rid).strip()
        if rid in answers:
            duplicates.add(rid)
        answers[rid] = value

    if isinstance(parsed.get('results'), list):
        for item in parsed['results']:
            if not isinstance(item, dict) or 'id' not in item:
                continue
            if multi_lang:
                trans = item.get('translations')
                if isinstance(trans, list):
                    try:
                        put(item['id'], _translation_list(trans))
                    except ValueError:
                        put(item['id'], _DUPLICATE)
                elif isinstance(trans, dict):
                    put(item['id'], trans)
            elif 'value' in item:
                put(item['id'], item['value'])
    elif multi_lang and parsed and next(iter(parsed)) in set(lang_codes):
        # lang-first fallback: {"en_US": {"12": "..."}, "es_AR": {"12": "..."}}
        for lang_code, by_record in parsed.items():
            if isinstance(by_record, dict):
                for rid, value in by_record.items():
                    answers.setdefault(str(rid), {})[lang_code] = value
    else:
        # record-first fallback: {"12": value} or {"12": {"en_US": ...}}
        for rid, value in parsed.items():
            put(rid, value)

    for rid in duplicates:
        answers[rid] = _DUPLICATE
    unexpected = set(answers) - {str(rid) for rid in record_ids}
    if unexpected:
        _logger.warning("os_ai_fields: ignoring answers for unexpected record IDs %s", sorted(unexpected))
    return answers


def _parse_datetime(text):
    """ISO 8601 date and time; an offset is converted to UTC (stored naive, like Odoo)."""
    text = text.strip()
    if text[-1:] in ('Z', 'z'):
        text = text[:-1] + '+00:00'
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('expected a date and time (ISO 8601), got %r' % text) from None
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _parse_date(text):
    """ISO 8601 date (YYYY-MM-DD); a date and time is accepted only at midnight."""
    text = text.strip()
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        pass
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('expected a date (YYYY-MM-DD), got %r' % text) from None
    if value.time() != datetime.time(0, 0) or value.tzinfo is not None:
        raise ValueError('expected a date (YYYY-MM-DD), got %r' % text)
    return value.date()


def _single_number(text):
    numbers = _NUMBER_RE.findall(str(text))
    if len(numbers) != 1:
        raise ValueError('expected one number, got %r' % (text,))
    return float(numbers[0])


def _to_record_id(value):
    if isinstance(value, bool):
        raise ValueError('expected a record ID, got %r' % (value,))
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    raise ValueError('expected a record ID, got %r' % (value,))


def _convert_value(record, field_obj, value, candidates=None):
    """Validate an LLM value for ``field_obj`` and convert it to a storable value.

    Args:
        candidates (set): allowed record IDs for relational fields.

    Returns:
        The value to assign. An explicit null gives False ([] for many2many).

    Raises:
        ValueError: the value cannot be trusted (wrong type, unknown selection
        key, record outside the candidates...).
    """
    field_type = field_obj.type

    if field_type in RELATIONAL_FIELD_TYPES and candidates is None:
        raise ValueError('no candidate list for a relational field')

    if value is None:
        return [] if field_type == 'many2many' else False

    if field_type in TEXT_FIELD_TYPES:
        if isinstance(value, (dict, list)):
            raise ValueError('expected text, got %s' % type(value).__name__)
        return str(value)

    if field_type == 'integer':
        if isinstance(value, bool):
            raise ValueError('expected an integer, got %r' % value)
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            number = _single_number(value)
        if not math.isfinite(number):
            raise ValueError('expected a finite number')
        number = int(round(number))
        if not INT32_MIN <= number <= INT32_MAX:
            raise ValueError('%s is out of the integer range' % number)
        return number

    if field_type in ('float', 'monetary'):
        if isinstance(value, bool):
            raise ValueError('expected a number, got %r' % value)
        number = float(value) if isinstance(value, (int, float)) else _single_number(value)
        if not math.isfinite(number):
            raise ValueError('expected a finite number')
        return number

    if field_type == 'boolean':
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        word = str(value).strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError('expected a boolean, got %r' % (value,))

    if field_type == 'selection':
        options = field_obj._description_selection(record.env)
        text = str(value).strip()
        if not text:
            return False
        for key, _label in options:
            if str(key) == text:
                return key
        lowered = text.lower()
        for attr in (0, 1):  # case-insensitive match on keys, then on labels
            matches = [opt[0] for opt in options if str(opt[attr]).strip().lower() == lowered]
            if len(matches) == 1:
                return matches[0]
        raise ValueError('%r is not one of the options %s' % (value, [key for key, _label in options]))

    if field_type == 'many2one':
        if isinstance(value, str) and value.strip().lower() in NONE_WORDS:
            return False
        if value is False or value == 0:
            return False
        rec_id = _to_record_id(value)
        if rec_id not in candidates:
            raise ValueError('record %s is not one of the allowed options' % rec_id)
        return rec_id

    if field_type == 'many2many':
        if isinstance(value, (list, tuple)):
            items = list(value)
        else:
            text = str(value).strip()
            if text.upper() in ('', 'NONE', 'NULL'):
                return []
            items = [part for part in re.split(r'[,;\s]+', text) if part]
        ids = []
        for item in items:
            rec_id = _to_record_id(item)
            if rec_id not in candidates:
                raise ValueError('record %s is not one of the allowed options' % rec_id)
            if rec_id not in ids:
                ids.append(rec_id)
        return ids

    if field_type == 'date':
        if not isinstance(value, str):
            raise ValueError('expected a date, got %r' % (value,))
        return _parse_date(value)

    if field_type == 'datetime':
        if not isinstance(value, str):
            raise ValueError('expected a datetime, got %r' % (value,))
        return _parse_datetime(value)

    return value


# ---------------------------------------------------------------------------
# 7) Prompt and schema builders
# ---------------------------------------------------------------------------

def _value_schema(env, field_obj, candidate_ids=None):
    """JSON schema of one field value.

    Closed sets (selection keys, relational candidates) are enums so that
    providers with strict structured outputs can only answer a valid option.
    "No value" is a string sentinel ("" / "0") rather than null: nullable enums
    are not handled the same way by every provider.
    """
    field_type = field_obj.type
    if field_type == 'selection':
        keys = [str(key) for key, _label in field_obj._description_selection(env)]
        return {"type": "string", "enum": keys + [""]}
    if field_type == 'many2one':
        return {"type": "string", "enum": ["0"] + [str(rid) for rid in sorted(candidate_ids or ())]}
    if field_type == 'many2many':
        return {"type": "array", "items": {"type": "string", "enum": [str(rid) for rid in sorted(candidate_ids or ())]}}
    if field_type == 'integer':
        return {"type": ["integer", "null"]}
    if field_type in ('float', 'monetary'):
        return {"type": ["number", "null"]}
    if field_type == 'boolean':
        return {"type": ["boolean", "null"]}
    if field_type == 'date':
        return {"type": ["string", "null"], "description": "A date: YYYY-MM-DD"}
    if field_type == 'datetime':
        return {"type": ["string", "null"], "description": (
            "A date and time in ISO 8601 with its UTC offset, e.g. 2026-10-24T12:00:00+03:00 "
            "(read as UTC without offset)")}
    return {"type": ["string", "null"]}


def _build_selection_options(env, field_obj):
    lines = ['Available options (reply ONLY with the key, or an empty string for none):']
    for key, label in field_obj._description_selection(env):
        lines.append('- %s: %s' % (key, label))
    return '\n'.join(lines)


def _build_json_schema(value_schema, is_batch, is_translatable, lang_codes, doc_type=None):
    """Build a LiteLLM response_format JSON schema based on field type and context."""

    # -- Document fields: special schemas ----------------------------------
    if doc_type == 'xlsx':
        # LLM must return a JSON object with headers + rows.
        # Cell values are strings — json_to_xlsx auto-converts numerics.
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "xlsx_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "headers": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "string"}
                            }
                        }
                    },
                    "required": ["headers", "rows"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
    elif doc_type == 'pdf':
        # LLM must return an HTML string in {"value": "<html>..."}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "pdf_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"}
                    },
                    "required": ["value"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }

    # -- Standard fields ---------------------------------------------------
    if is_batch and is_translatable and len(lang_codes) > 1:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "batch_translation_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "translations": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "lang_code": {"type": "string"},
                                                "value": value_schema
                                            },
                                            "required": ["lang_code", "value"],
                                            "additionalProperties": False
                                        }
                                    }
                                },
                                "required": ["id", "translations"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["results"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
    elif is_batch:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "batch_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "value": value_schema
                                },
                                "required": ["id", "value"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["results"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
    elif is_translatable and len(lang_codes) > 1:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "translation_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "translations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "lang_code": {"type": "string"},
                                    "value": value_schema
                                },
                                "required": ["lang_code", "value"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["translations"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }
    else:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "single_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "value": value_schema
                    },
                    "required": ["value"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }


def _build_batch_prompt(record_prompts):
    """Build a batch user prompt asking for JSON-keyed results."""
    lines = [
        "Compute the field value for each record below.",
        "Return ONLY a valid JSON object with a 'results' array.",
        "Each item in the array must have an 'id' (record ID as string) and a 'value' (computed value).",
        "Do NOT include any text outside the JSON. No markdown, no code fences, no explanations.",
        "",
        "Records:",
    ]
    for rid, prompt in record_prompts.items():
        lines.append(f'- ID "{rid}": {prompt}')
    lines.append("")
    lines.append("Expected response format:")
    example_ids = list(record_prompts.keys())[:2]
    example = {"results": [{"id": rid, "value": "..."} for rid in example_ids]}
    lines.append(json.dumps(example))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8) Translation helpers
# ---------------------------------------------------------------------------

def _build_translation_suffix(env):
    """Return a prompt suffix requesting multi-language JSON output.

    Used for SINGLE-record translatable fields.

    Returns:
        tuple: (suffix_string, list_of_lang_codes)
    """
    installed = env['res.lang'].get_installed()  # [(code, name), ...]
    if len(installed) <= 1:
        return '', [installed[0][0]] if installed else ['en_US']

    lang_dict = {code: name for code, name in installed}
    suffix = (
        "Generate the response in each of these languages and return ONLY "
        "a valid JSON object with a 'translations' array.\n"
        "Each item must have a 'lang_code' and a 'value'.\n"
        "Languages: %s\n"
        'Expected format: {"translations": [{"lang_code": "en_US", "value": "..."}, {"lang_code": "es_AR", "value": "..."}]}'
    ) % (json.dumps(lang_dict, ensure_ascii=False))
    return suffix, list(lang_dict.keys())


def _build_batch_translation_suffix(lang_codes, record_prompts):
    """Return a prompt suffix for BATCH + translatable fields.

    Instructs the LLM to return per-record, per-language nested JSON.
    """
    example_ids = list(record_prompts.keys())[:2]
    example = {
        "results": [
            {
                "id": rid,
                "translations": [{"lang_code": lc, "value": "..."} for lc in lang_codes]
            }
            for rid in example_ids
        ]
    }
    return (
        "IMPORTANT: For each record, generate the value in ALL these languages.\n"
        "Return ONLY a JSON object with a 'results' array. Each item must have the record 'id' and a 'translations' array.\n"
        "Languages: %s\n"
        "Override the expected format above. Use this format instead:\n%s"
    ) % (
        ', '.join(lang_codes),
        json.dumps(example, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# 9) Relational field helpers
# ---------------------------------------------------------------------------

Candidates = namedtuple('Candidates', 'status ids suffix message records')


def _candidates_depend_on_company(env, field_obj):
    """True when check_company restricts the candidates (as the ORM of this Odoo version does).

    Odoo 18+ applies the comodel's own rule (_check_company_domain, e.g.
    company_ids for accounts); older versions only comodels with company_id.
    """
    Comodel = env.get(field_obj.comodel_name)
    if not getattr(field_obj, 'check_company', False) or Comodel is None:
        return False
    return odoo_release.version_info[0] >= 18 or 'company_id' in Comodel._fields


def _company_domain(record, field_obj, Comodel):
    """Odoo's check_company rule for the candidates of ``field_obj`` on ``record``.

    Shared records plus those of the record's company (the current company for
    company-dependent fields); users qualify when the company is one of theirs.
    """
    if getattr(field_obj, 'company_dependent', False):
        companies = record.env.company
    elif record._name == 'res.company':
        companies = record
    elif 'company_id' in record._fields:
        companies = record.company_id
    elif odoo_release.version_info[0] >= 18 and 'company_ids' in record._fields:
        companies = record.company_ids
    else:
        return []  # no company on the record: Odoo skips the check
    if hasattr(Comodel, '_check_company_domain'):  # Odoo 17+: the rule of each model
        return list(Comodel._check_company_domain(companies))
    if Comodel._name == 'res.users':  # Odoo 15/16: users may work for several companies
        return [('company_ids', 'in', companies.ids)] if companies else []
    if not companies:
        return [('company_id', '=', False)]
    return ['|', ('company_id', '=', False), ('company_id', 'in', companies.ids)]


def _has_static_domain(field_obj):
    """True when the field's domain does not depend on the record."""
    domain = getattr(field_obj, 'domain', None)
    return not (callable(domain) or isinstance(domain, str))


def _evaluate_domain(field_obj, record):
    """Evaluate the field's native domain for ``record``."""
    domain = getattr(field_obj, 'domain', None) or []
    if callable(domain):
        domain = domain(record)
    if isinstance(domain, str):
        # client-side domain, e.g. "[('company_id', '=', company_id)]"
        context = {'uid': record.env.uid, 'context_today': odoo_fields.Date.context_today(record)}
        for name in set(re.findall(r'\b[a-z_][a-z0-9_]*\b', domain)):
            field = record._fields.get(name)
            if field is None:
                continue
            value = record[name]
            if field.type == 'many2one':
                value = value.id
            elif field.type in ('one2many', 'many2many'):
                value = value.ids
            context[name] = value
        domain = safe_eval(domain, context)
    return list(domain or [])


def get_relational_candidates(env, field_obj, record, max_options=None):
    """Records the LLM may choose for a relational field.

    Uses the field's native domain evaluated for ``record`` and, for fields
    with check_company=True, the record's company. Fails closed: when the
    candidates cannot be determined, nothing may be selected.

    The search runs as superuser only for fields computed as superuser
    (compute_sudo, the default of AI fields); otherwise the record rules of
    the computing user apply, so that hidden records never reach the prompt.

    Returns:
        Candidates: status 'ok', 'empty', 'too_many' or 'error'.
    """
    field_label = '%s.%s' % (field_obj.model_name, field_obj.name)
    Comodel = env.get(field_obj.comodel_name)
    if Comodel is None:
        return Candidates('error', frozenset(), '', 'unknown model %s for %s' % (field_obj.comodel_name, field_label), None)

    limit = getattr(field_obj, 'ai_compute_options_limit', None) or DEFAULT_OPTIONS_LIMIT
    if max_options:
        limit = min(limit, max_options)
    try:
        with env.cr.savepoint(flush=False):
            domain = _evaluate_domain(field_obj, record)
            if _candidates_depend_on_company(env, field_obj):
                company_domain = _company_domain(record, field_obj, Comodel)
                if company_domain:
                    domain = ['&'] + company_domain + (domain or [(1, '=', 1)])
            if getattr(field_obj, 'compute_sudo', True):
                Comodel = Comodel.sudo()
            options = Comodel.search(domain, limit=limit + 1)
    except Exception as exc:
        return Candidates('error', frozenset(), '', 'could not evaluate the domain of %s: %s' % (field_label, exc), None)

    if not options:
        return Candidates('empty', frozenset(), '', 'no record matches the domain of %s' % field_label, None)
    if len(options) > limit:
        return Candidates('too_many', frozenset(), '', (
            'more than %d records match the domain of %s; narrow the domain or raise '
            'ai_compute_options_limit' % (limit, field_label)), None)

    lines = []
    if field_obj.type == 'many2one':
        lines.append('Available options (reply ONLY with the ID of your choice, or "0" for none):')
    else:
        lines.append('Available options (reply ONLY with the list of the IDs you choose, or an empty list):')
    for rec in options:
        lines.append('- %d: %s' % (rec.id, rec.display_name or ''))
    return Candidates('ok', frozenset(options.ids), '\n'.join(lines), None, options)


# ---------------------------------------------------------------------------
# 10) Decision engine (typed decision models, e.g. TypeSafe Jev)
# ---------------------------------------------------------------------------

def _decision_request(record, field_obj, template, candidate_records=None):
    """Build the decision request of one record.

    The prompt placeholders become the keys of ``state`` (the record data) and
    are referenced by name in the instructions, which therefore contain no
    record data.

    Returns:
        tuple: (state, questions, options) where options maps each answer key
        to the value to store (None for Boolean fields).
    """
    from .helpers import resolve_prompt

    state = {}
    keys = {}  # placeholder -> state key: distinct placeholders never share a key

    def to_reference(match):
        token = match.group(1).strip()
        if token not in keys:
            base = re.sub(r'\W+', '_', token).strip('_') or 'value'
            key, suffix = base, 2
            while key in state:
                key, suffix = '%s_%d' % (base, suffix), suffix + 1
            keys[token] = key
            state[key] = resolve_prompt('{%s}' % token, record)[0]
        return '`%s`' % keys[token]

    instructions = _PLACEHOLDER_RE.sub(to_reference, template or '')

    if field_obj.type == 'boolean':
        return state, {'value': {'type': 'noul', 'instructions': instructions}}, None

    if field_obj.type == 'selection':
        # English labels: the language the decision model is best at
        pairs = field_obj._description_selection(record.with_context(lang='en_US').env)
        criteria = {str(key): label for key, label in pairs}
        options = {str(key): key for key, _label in pairs}
        if DECISION_NONE in criteria:
            raise ValueError('the selection key %s is reserved for "none of the options"' % DECISION_NONE)
    else:
        criteria = {str(rec.id): rec.display_name for rec in candidate_records}
        options = {str(rec.id): rec.id for rec in candidate_records}
    if len(criteria) + 1 > DECISION_MAX_OPTIONS:
        raise ValueError('more than %d options' % (DECISION_MAX_OPTIONS - 1))
    criteria[DECISION_NONE] = 'None of the other options applies'
    return state, {'value': {'type': 'choice', 'instructions': instructions, 'criteria': criteria}}, options


def _probability(value, name):
    """A probability or confidence from the decision model: a number in [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('%s is not a number: %r' % (name, value))
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError('%s out of range: %r' % (name, value))
    return value


def _check_decision_parameters(field_obj):
    """Developer parameters of a decision field. Returns an error message or None."""
    for param in ('ai_decision_threshold', 'ai_decision_margin', 'ai_decision_min_confidence'):
        try:
            _probability(getattr(field_obj, param), param)
        except ValueError as exc:
            return str(exc)
    if field_obj.ai_decision_margin > min(field_obj.ai_decision_threshold, 1 - field_obj.ai_decision_threshold):
        return 'ai_decision_margin leaves no room for a yes or a no around ai_decision_threshold'
    return None


def _interpret_decision(field_obj, answer, options):
    """Turn a decision answer into (outcome, value, detail).

    An uncertain answer is an abstention (ABSTAINED): the stored value is kept,
    which is different from answering False or "none".
    """
    if not isinstance(answer, dict):
        raise ValueError('answer is not an object')
    if field_obj.type == 'boolean':
        probability = _probability(answer.get('noul'), 'noul')
        threshold = field_obj.ai_decision_threshold
        margin = field_obj.ai_decision_margin
        detail = 'p(yes)=%.2f' % probability
        if probability >= threshold + margin:
            return DONE, True, detail
        if probability <= threshold - margin:
            return DONE, False, detail
        return ABSTAINED, None, 'uncertain answer, %s' % detail

    choice = answer.get('choice')
    if not isinstance(choice, str) or (choice != DECISION_NONE and choice not in options):
        raise ValueError('unknown option %r' % (choice,))
    confidence = _probability(answer.get('confidence'), 'confidence')
    detail = 'choice=%s confidence=%.2f' % (choice, confidence)
    if confidence < field_obj.ai_decision_min_confidence:
        return ABSTAINED, None, 'low confidence, %s' % detail
    if choice == DECISION_NONE:
        return DONE, False, detail
    return DONE, options[choice], detail


def _compute_decisions(env, plan, batch, use_savepoint):
    """Compute Boolean / Selection / Many2one fields with a decision model."""
    field_name = plan['field_name']
    field_obj = plan['field_obj']
    provider = plan['provider']
    results = {}
    for record in batch:
        candidate_records = None
        if field_obj.type == 'many2one':
            candidates = get_relational_candidates(env, field_obj, record, max_options=DECISION_MAX_OPTIONS - 1)
            if candidates.status != 'ok':
                outcome = RETRYABLE if candidates.status == 'empty' else PERMANENT
                results[record.id] = (outcome, candidates.message)
                continue
            candidate_records = candidates.records
        try:
            state, questions, options = _decision_request(record, field_obj, plan['prompt_template'], candidate_records)
        except ValueError as exc:
            results[record.id] = (PERMANENT, 'Cannot ask the decision model: %s' % exc)
            continue

        result = provider.sudo().call_decision(state, questions)
        log_vals = _log_call(env, plan, record._name, field_name, [record.id], '',
                             json.dumps({'state': state, 'questions': questions}, ensure_ascii=False, default=str),
                             result, 1)
        if result['success']:
            try:
                outcome, value, detail = _interpret_decision(field_obj, result['answers']['value'], options)
            except (KeyError, TypeError, ValueError) as exc:
                res = (RETRYABLE, 'Invalid answer: %s' % exc)
            else:
                if outcome == DONE:
                    res = _store_value(env, record, field_name, value, False, use_savepoint)
                else:
                    res = (outcome, detail)
        else:
            res = _call_failure(result)
        results[record.id] = res
        if res[0] != DONE:
            _logger.info("os_ai_fields: %s.%s id=%s not updated by the decision model (%s): %s",
                         record._name, field_name, record.id, res[0], res[1])
        _log_outcomes(env, plan, log_vals, {record.id: res})
    return results


# ---------------------------------------------------------------------------
# 11) Logging
# ---------------------------------------------------------------------------

def _log_call(env, plan, model_name, field_name, record_ids, system_prompt, user_prompt, result, batch_size):
    """Values of the AI Log of a call, right after the call.

    In the cron (``plan['log_sink']``) they are collected at once, so that the
    call is logged even if storing its answer is rolled back (e.g. a record
    changed meanwhile). Completed by _log_outcomes().
    """
    AiLog = env.get('os.ai.log')
    if AiLog is None:
        return None
    try:
        raw_json = json.dumps(result.get('raw_response', {}), default=str, ensure_ascii=False)
    except Exception:
        raw_json = str(result.get('raw_response', ''))
    vals = {
        'model_name': model_name,
        'field_name': field_name,
        'res_ids': json.dumps(record_ids),
        'provider_id': plan['provider'].id,
        'provider_model': result.get('model', ''),
        'system_prompt': system_prompt or '',
        'user_prompt': user_prompt or '',
        'response_content': result.get('content', ''),
        'response_raw': raw_json,
        'success': result.get('success', False),
        'error_message': result.get('error') or '',
        'prompt_tokens': result.get('prompt_tokens', 0),
        'completion_tokens': result.get('completion_tokens', 0),
        'total_tokens': result.get('total_tokens', 0),
        'response_cost': result.get('cost', 0.0),
        'cost_status': result.get('cost_status') or 'unknown',
        'duration': result.get('duration', 0.0),
        'batch_size': batch_size,
    }
    if plan.get('log_sink') is not None:
        plan['log_sink'].append(vals)
    return vals


def _log_outcomes(env, plan, vals, outcomes):
    """Add the outcome per record to the log values; create the log outside the cron."""
    AiLog = env.get('os.ai.log')
    if vals is None or AiLog is None:
        return
    if 'applied_count' in AiLog._fields:
        failed = {rid: res for rid, res in outcomes.items() if res[0] != DONE}
        vals.update({
            'applied_count': len(outcomes) - len(failed),
            'failed_count': len(failed),
            'apply_errors': '\n'.join(
                'id %s (%s): %s' % (rid, outcome, message) for rid, (outcome, message) in failed.items()
            ),
        })
    if plan.get('log_sink') is None:
        try:
            AiLog.sudo().create(vals)
        except Exception as exc:
            _logger.error("os_ai_fields: failed to create ai.log: %s", exc)


_logger.debug("os_ai_fields: patched odoo.fields.Field with ai_compute / ai_compute_depends / ai_compute_async / ai_compute_batch_size")
# Translation support: fields with translate=True and ai_compute automatically
# compute in all installed languages using a single multi-language LLM call.
