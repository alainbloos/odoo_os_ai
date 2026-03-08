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
"""

import json
import logging
import re

from odoo import fields as odoo_fields
from odoo import models as odoo_models
from odoo import api

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1) Add new class-level attributes to Field
# ---------------------------------------------------------------------------
odoo_fields.Field.ai_compute = None           # prompt template string
odoo_fields.Field.ai_compute_depends = None    # list of dependency field names
odoo_fields.Field.ai_compute_async = True      # True = cron-based (default), False = blocking
odoo_fields.Field.ai_compute_batch_size = 40   # max records per LLM call
odoo_fields.Field.ai_document_type = None      # 'pdf', 'xlsx' — for Binary fields that generate documents

# ---------------------------------------------------------------------------
# 2) Make _valid_field_parameter accept the new keys
# ---------------------------------------------------------------------------
_AI_FIELD_PARAMS = frozenset({
    'ai_compute', 'ai_compute_depends', 'ai_compute_async',
    'ai_compute_batch_size', 'ai_document_type',
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
_ai_compute_methods = {}
_pending_fields_injected = set()  # track (model_name, field_name) to avoid duplicates


def _patched_get_attrs(self, model_class, name):
    attrs = _original_get_attrs(self, model_class, name)

    ai_compute_prompt = attrs.get('ai_compute')
    if ai_compute_prompt:
        ai_depends = attrs.get('ai_compute_depends') or []
        ai_async = attrs.get('ai_compute_async', True)
        ai_batch_size = attrs.get('ai_compute_batch_size', 40)

        compute_name = '_compute_ai_%s' % name
        model_name = model_class._name

        key = (model_name, name)
        if key not in _ai_compute_methods:
            _ai_compute_methods[key] = _create_ai_compute_method(
                compute_name, name, ai_async, ai_batch_size,
            )

        compute_method = _ai_compute_methods[key]

        if ai_depends:
            compute_method = api.depends(*ai_depends)(compute_method)

        setattr(model_class, compute_name, compute_method)

        attrs['compute'] = compute_name
        attrs['store'] = attrs.get('store', True)
        attrs['readonly'] = attrs.get('readonly', True)
        attrs['compute_sudo'] = attrs.get('compute_sudo', True)
        if not (attrs['store'] and not attrs.get('readonly', True)):
            attrs['copy'] = attrs.get('copy', False)

        # -- Auto-inject _pending companion field for async fields --
        if ai_async and key not in _pending_fields_injected:
            pending_name = '%s_pending' % name
            if not hasattr(model_class, pending_name):
                pending_field = odoo_fields.Boolean(
                    string='%s Pending' % name.replace('_', ' ').title(),
                    default=False,
                    store=True,
                    readonly=True,
                    copy=False,
                )
                model_class._columns = getattr(model_class, '_columns', {})
                setattr(model_class, pending_name, pending_field)
                # Also trigger __set_name__ so Odoo knows about the field
                pending_field.__set_name__(model_class, pending_name)
                _logger.debug(
                    "os_ai_fields: injected companion field %s.%s",
                    model_name, pending_name,
                )
            _pending_fields_injected.add(key)

        _logger.debug(
            "os_ai_fields: injected compute=%s on %s.%s (store=%s, depends=%s, async=%s, batch=%d)",
            compute_name, model_name, name, attrs.get('store'), ai_depends, ai_async, ai_batch_size,
        )

    return attrs

odoo_fields.Field._get_attrs = _patched_get_attrs


# ---------------------------------------------------------------------------
# 4) Compute method factory
# ---------------------------------------------------------------------------

def _create_ai_compute_method(compute_name, field_name, is_async, batch_size):
    """Create a compute method for an AI field.

    If is_async=True, the method only sets _pending=True and returns.
    The actual LLM call is done by the cron.
    """
    from .helpers import resolve_prompt
    from .models.res_config_settings import DEFAULT_SYSTEM_PROMPT

    def _compute_ai(self):
        records = self

        # Skip LLM calls during onchange — records have NewId, not real IDs.
        if records and not all(isinstance(r.id, int) and r.id for r in records):
            _logger.debug("os_ai_fields: skipping %s.%s — onchange context (NewId)",
                           self._name, field_name)
            return

        # -- ASYNC: just mark as pending and return --
        if is_async:
            pending_name = '%s_pending' % field_name

            # During module install/update, Odoo triggers _recompute_all()
            # which calls our compute. Writing to pending here would trigger
            # cascading writes (mail tracking, etc.) that crash the init.
            # Use env.cache to set the value without triggering write().
            pending_field_obj = self._fields.get(pending_name)
            if pending_field_obj:
                _logger.debug("os_ai_fields: marking %d record(s) as pending for %s.%s",
                               len(records), self._name, field_name)
                for record in records:
                    self.env.cache.set(record, pending_field_obj, True)
            return

        # -- SYNC: compute immediately --
        _logger.debug("os_ai_fields: computing %s.%s for %d record(s) (batch_size=%d)",
                       self._name, field_name, len(records), batch_size)

        system_prompt = self.env['ir.config_parameter'].sudo().get_param(
            'os_ai_fields.system_prompt', DEFAULT_SYSTEM_PROMPT,
        )

        target_field_obj = self._fields.get(field_name)
        target_field_type = target_field_obj.type if target_field_obj else 'char'

        prompt_template = _resolve_prompt_template(self.env, self._name, field_name, target_field_obj)

        if not prompt_template:
            _logger.warning("os_ai_fields: no prompt found for %s.%s", self._name, field_name)
            for record in records:
                record[field_name] = ''
            return

        # Dynamically determine needed capability based on target field and prompt tokens
        cap = _detect_required_capability(self.env, self._name, target_field_type, prompt_template, field_obj=target_field_obj)

        provider = _get_provider(self.env, required_capability=cap)
        if not provider:
            for record in records:
                record[field_name] = ''
            return

        # Override batch size. Image capabilities and document fields do not support batching.
        actual_batch_size = batch_size
        if cap != 'capable_text' or getattr(target_field_obj, 'ai_document_type', None):
            actual_batch_size = 1

        AiLog = self.env.get('os.ai.log')
        record_list = list(records)

        for i in range(0, len(record_list), actual_batch_size):
            batch = record_list[i:i + actual_batch_size]
            _compute_batch(
                self.env, provider, system_prompt, prompt_template,
                field_name, batch, AiLog, cap
            )

    _compute_ai.__name__ = compute_name
    return _compute_ai


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
        # For PDF, extract HTML from {"value": "<html>..."} wrapper
        html_str = content_str
        try:
            cleaned = content_str.strip()
            cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\s*```$', '', cleaned)
            js = json.loads(cleaned.strip())
            if isinstance(js, dict) and 'value' in js:
                html_str = js['value']
        except (json.JSONDecodeError, TypeError):
            pass  # Use raw content as HTML
        return document_utils.html_to_pdf(env, html_str)
    else:
        _logger.error("os_ai_fields: unknown document type '%s'", doc_type)
        return False


def _compute_batch(env, provider, system_prompt, prompt_template, field_name, batch, AiLog, cap):
    """Compute a batch of records in a single LLM call."""
    from .helpers import resolve_prompt

    model_name = batch[0]._name
    record_ids = [r.id.origin if hasattr(r.id, 'origin') else (r.id or 0) for r in batch]
    field_obj = batch[0]._fields.get(field_name)
    field_type = field_obj.type if field_obj else 'char'

    # Detect translatable fields
    is_translatable = bool(getattr(field_obj, 'translate', False))
    translation_suffix = ''
    lang_codes = []
    if is_translatable:
        translation_suffix, lang_codes = _build_translation_suffix(env)

    # Detect document type (pdf, xlsx)
    doc_type = getattr(field_obj, 'ai_document_type', None) if field_obj else None

    # For relational fields, fetch available options once per batch
    options_suffix = ''
    valid_ids = set()
    if field_type in ('many2one', 'many2many') and field_obj:
        options_suffix, valid_ids = _build_relational_options(
            env, field_obj, field_type,
        )

    schema = _build_json_schema(field_type, len(batch) > 1, is_translatable, lang_codes, doc_type=doc_type)

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
        resolved, images_list = resolve_prompt(prompt_template, record)
        if options_suffix:
            resolved = resolved + '\n\n' + options_suffix
        if translation_suffix:
            resolved = resolved + '\n\n' + translation_suffix
        if doc_suffix:
            resolved = resolved + '\n\n' + doc_suffix

        _logger.debug("os_ai_fields: calling LLM for %s.%s (id=%s) [single schema], cap=%s", model_name, field_name, record.id, cap)
        
        call_kwargs = {
            'system_prompt': system_prompt,
            'user_prompt': resolved,
            'response_format': schema if cap in ('capable_text', 'capable_vision') else None,
            'images': images_list,
            'capability_used': cap,
        }
        
        result = provider.sudo().call_llm(**call_kwargs)

        if result['success']:
            content_str = result['content']

            # -- Document post-processing: convert LLM text → binary ------
            if doc_type:
                binary_value = _postprocess_document(env, doc_type, content_str)
                if binary_value:
                    record[field_name] = binary_value
                else:
                    _logger.error("os_ai_fields: document post-processing failed for %s.%s (type=%s)",
                                  model_name, field_name, doc_type)
                    record[field_name] = False
            else:
                parsed_value = content_str
                if not (is_translatable and len(lang_codes) > 1) and content_str.strip().startswith('{'):
                    try:
                        js = json.loads(content_str)
                        if isinstance(js, dict) and 'value' in js:
                            parsed_value = js['value']
                    except Exception:
                        pass

                if is_translatable and len(lang_codes) > 1:
                    _assign_translated_values(record, field_name, field_type,
                                              content_str, lang_codes, valid_ids)
                else:
                    _assign_field_value(record, field_name, field_type, parsed_value, valid_ids)
        else:
            _logger.error("os_ai_fields: LLM call failed for %s.%s: %s",
                          model_name, field_name, result['error'])

        _create_log(env, AiLog, model_name, field_name, record_ids,
                     provider, system_prompt, resolved, result, len(batch))
    else:
        record_prompts = {}
        for record in batch:
            resolved, images_list = resolve_prompt(prompt_template, record)
            record_prompts[str(record.id)] = resolved

        batch_user_prompt = _build_batch_prompt(record_prompts)
        if options_suffix:
            batch_user_prompt = batch_user_prompt + '\n\n' + options_suffix

        # For batch + translatable: use a special suffix that instructs
        # per-record per-language nesting
        if is_translatable and len(lang_codes) > 1:
            batch_trans_suffix = _build_batch_translation_suffix(lang_codes, record_prompts)
            batch_user_prompt = batch_user_prompt + '\n\n' + batch_trans_suffix

        _logger.debug("os_ai_fields: calling LLM for %s.%s batch of %d records [batch schema], cap=%s",
                       model_name, field_name, len(batch), cap)
                       
        call_kwargs = {
            'system_prompt': system_prompt,
            'user_prompt': batch_user_prompt,
            'response_format': schema,
            'images': None, # Batches are text only due to upstream restriction
            'capability_used': cap,
        }
        
        result = provider.sudo().call_llm(**call_kwargs)

        if result['success']:
            if is_translatable and len(lang_codes) > 1:
                _assign_batch_translated_values(
                    batch, field_name, field_type,
                    result['content'], record_ids, lang_codes, valid_ids,
                )
            else:
                parsed = _parse_batch_response(result['content'], record_ids)
                for record in batch:
                    value = parsed.get(str(record.id), '')
                    _assign_field_value(record, field_name, field_type, value, valid_ids)
                    if not value:
                        _logger.warning("os_ai_fields: no value returned for %s.%s id=%s",
                                         model_name, field_name, record.id)
        else:
            _logger.error("os_ai_fields: batch LLM call failed for %s.%s: %s",
                          model_name, field_name, result['error'])

        _create_log(env, AiLog, model_name, field_name, record_ids,
                     provider, system_prompt, batch_user_prompt, result, len(batch))


def _build_json_schema(field_type, is_batch, is_translatable, lang_codes, doc_type=None):
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
    if field_type in ('integer', 'many2one'):
        value_schema = {"type": ["integer", "null"]}
    elif field_type in ('float', 'monetary'):
        value_schema = {"type": ["number", "null"]}
    elif field_type == 'boolean':
        value_schema = {"type": ["boolean", "null"]}
    else:
        value_schema = {"type": ["string", "null"]}

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


def _parse_batch_response(content, record_ids):
    """Parse a batch JSON response from the LLM."""
    cleaned = content.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "results" in parsed and isinstance(parsed["results"], list):
            res = {}
            for item in parsed["results"]:
                if "id" in item and "value" in item:
                    res[str(item["id"])] = item["value"]
            return res
        elif isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items()}
        else:
            _logger.error("os_ai_fields: batch response is not a dict: %s", type(parsed))
            return {}
    except json.JSONDecodeError as exc:
        _logger.error("os_ai_fields: failed to parse batch JSON response: %s\nContent: %s",
                       exc, content[:500])
        return {}


# ---------------------------------------------------------------------------
# 7) Translation helpers
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


def _assign_batch_translated_values(batch, field_name, field_type, content,
                                     record_ids, lang_codes, valid_ids):
    """Parse batch+translation nested JSON and assign all translations.

    Handles schema format as well as fallback formats.
    """
    content = (content or '').strip()
    if not content:
        return

    cleaned = re.sub(r'^```(?:json)?\s*', '', content, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        _logger.error("os_ai_fields: could not parse batch translation JSON for %s: %s",
                       field_name, content[:300])
        return

    if not isinstance(parsed, dict):
        _logger.error("os_ai_fields: batch translation response is not a dict")
        return

    record_translations = {}
    if "results" in parsed and isinstance(parsed["results"], list):
        for item in parsed["results"]:
            rid = str(item.get("id"))
            trans_list = item.get("translations", [])
            record_translations[rid] = {t["lang_code"]: t.get("value") for t in trans_list if "lang_code" in t and "value" in t}
    else:
        # Detect fallback format
        first_key = next(iter(parsed), '')
        str_record_ids = {str(rid) for rid in record_ids}

        if first_key in str_record_ids:
            record_translations = parsed
        elif first_key in set(lang_codes):
            for lang_code, records_dict in parsed.items():
                if isinstance(records_dict, dict):
                    for rid, value in records_dict.items():
                        record_translations.setdefault(str(rid), {})[lang_code] = value
        else:
            _logger.error("os_ai_fields: unrecognized batch translation format, first_key=%s", first_key)
            return

    for record in batch:
        rid = str(record.id)
        translations = record_translations.get(rid)
        if not translations or not isinstance(translations, dict):
            _logger.warning("os_ai_fields: no translations for %s.%s id=%s",
                            record._name, field_name, rid)
            continue
        for lang_code in lang_codes:
            value = translations.get(lang_code)
            if value and isinstance(value, str):
                try:
                    record.with_context(lang=lang_code)[field_name] = value
                except Exception as exc:
                    _logger.error(
                        "os_ai_fields: failed to assign translation %s for %s.%s id=%s: %s",
                        lang_code, record._name, field_name, rid, exc,
                    )
            else:
                _logger.warning("os_ai_fields: missing lang %s for %s.%s id=%s",
                                lang_code, record._name, field_name, rid)


def _assign_translated_values(record, field_name, field_type, content, lang_codes, valid_ids):
    """Parse JSON with lang→value and assign each translation.

    Works across all Odoo versions (15-18) using with_context(lang=X).
    """
    content = (content or '').strip()
    if not content:
        return

    # Clean markdown fences if present
    cleaned = re.sub(r'^```(?:json)?\s*', '', content, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()

    try:
        parsed_json = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        _logger.error("os_ai_fields: could not parse translation JSON for %s.%s: %s",
                       record._name, field_name, content[:200])
        # Fallback: assign raw content in the current language
        _assign_field_value(record, field_name, field_type, content, valid_ids)
        return

    if not isinstance(parsed_json, dict):
        _logger.error("os_ai_fields: translation response is not a dict for %s.%s",
                       record._name, field_name)
        _assign_field_value(record, field_name, field_type, content, valid_ids)
        return

    translations = {}
    if "translations" in parsed_json and isinstance(parsed_json["translations"], list):
        for item in parsed_json["translations"]:
            if "lang_code" in item and "value" in item:
                translations[item["lang_code"]] = item["value"]
    else:
        translations = parsed_json

    for lang_code in lang_codes:
        value = translations.get(lang_code)
        if value is not None:
            if not isinstance(value, str):
                value = str(value)
            try:
                record.with_context(lang=lang_code)[field_name] = value
            except Exception as exc:
                _logger.error(
                    "os_ai_fields: failed to assign translation %s for %s.%s: %s",
                    lang_code, record._name, field_name, exc,
                )
        else:
            _logger.warning("os_ai_fields: no translation for lang %s in %s.%s",
                            lang_code, record._name, field_name)


# ---------------------------------------------------------------------------
# 8) Relational field helpers
# ---------------------------------------------------------------------------

def _build_relational_options(env, field_obj, field_type):
    """Fetch available options for a relational field.

    Uses the field's native domain to filter records.

    Returns:
        tuple: (options_suffix_str, set_of_valid_ids)
    """
    comodel_name = field_obj.comodel_name
    if not comodel_name:
        return '', set()

    Comodel = env.get(comodel_name)
    if Comodel is None:
        return '', set()

    domain = getattr(field_obj, 'domain', None) or []
    # domain can be a callable (e.g. lambda self: [...])
    if callable(domain):
        try:
            domain = domain(Comodel)
        except Exception:
            domain = []

    try:
        options = Comodel.sudo().search(domain)
    except Exception as exc:
        _logger.error("os_ai_fields: failed to search %s with domain %s: %s",
                       comodel_name, domain, exc)
        return '', set()

    if not options:
        return '', set()

    valid_ids = set(options.ids)

    lines = []
    if field_type == 'many2one':
        lines.append('Available options (reply ONLY with the numeric ID of your choice, or 0 for none):')
    else:
        lines.append('Available options (reply ONLY with a comma-separated list of numeric IDs, or NONE):')

    for rec in options:
        lines.append('- %d: %s' % (rec.id, rec.display_name or ''))

    return '\n'.join(lines), valid_ids


def _assign_field_value(record, field_name, field_type, content, valid_ids):
    """Assign a value to a field, handling relational and typed fields."""
    if content is None:
        if field_type == 'many2many':
            record[field_name] = [(5, 0, 0)]
        else:
            record[field_name] = False
        return

    try:
        if field_type == 'many2one':
            if isinstance(content, int):
                rec_id = content
            else:
                raw = re.sub(r'[^0-9]', ' ', str(content)).strip().split()
                rec_id = int(raw[0]) if raw else 0
            if rec_id and valid_ids and rec_id not in valid_ids:
                _logger.warning(
                    "os_ai_fields: LLM returned id=%d not in valid options for %s.%s",
                    rec_id, record._name, field_name,
                )
                return
            record[field_name] = rec_id or False

        elif field_type == 'many2many':
            if isinstance(content, list):
                ids = [int(x) for x in content if str(x).isdigit()]
                if valid_ids:
                    invalid = [x for x in ids if x not in valid_ids]
                    if invalid:
                        _logger.warning("os_ai_fields: LLM returned invalid ids %s for %s.%s", invalid, record._name, field_name)
                    ids = [x for x in ids if x in valid_ids]
                record[field_name] = [(6, 0, ids)] if ids else [(5, 0, 0)]
            else:
                content_str = str(content).strip()
                if not content_str or content_str.upper() == 'NONE':
                    record[field_name] = [(5, 0, 0)]  # clear
                    return
                raw_ids = re.findall(r'\d+', content_str)
                ids = [int(x) for x in raw_ids]
                if valid_ids:
                    ids = [x for x in ids if x in valid_ids]
                record[field_name] = [(6, 0, ids)] if ids else [(5, 0, 0)]

        elif field_type == 'integer':
            if isinstance(content, (int, float)):
                record[field_name] = int(content)
            else:
                nums = re.findall(r'-?\d+', str(content))
                record[field_name] = int(nums[0]) if nums else 0

        elif field_type == 'float':
            if isinstance(content, (int, float)):
                record[field_name] = float(content)
            else:
                nums = re.findall(r'-?\d+\.?\d*', str(content))
                record[field_name] = float(nums[0]) if nums else 0.0

        elif field_type == 'boolean':
            if isinstance(content, bool):
                record[field_name] = content
            else:
                text = str(content).strip().lower()
                record[field_name] = text in ('true', '1', 'yes', 'y', 't')

        elif field_type == 'date':
            str_val = str(content).strip()
            record[field_name] = str_val if str_val else False

        elif field_type == 'datetime':
            str_val = str(content).strip()
            record[field_name] = str_val if str_val else False

        else:
            # char, text, html, selection, etc.
            record[field_name] = str(content)

    except Exception as exc:
        _logger.error(
            "os_ai_fields: failed to assign %s.%s (type=%s) value=%r: %s",
            record._name, field_name, field_type, content[:100], exc,
        )


def _create_log(env, AiLog, model_name, field_name, record_ids,
                provider, system_prompt, user_prompt, result, batch_size):
    """Create an ai.log entry for the LLM call."""
    if AiLog is None:
        return

    raw_json = ''
    try:
        raw_json = json.dumps(result.get('raw_response', {}), default=str, ensure_ascii=False)
    except Exception:
        raw_json = str(result.get('raw_response', ''))

    vals = {
        'model_name': model_name,
        'field_name': field_name,
        'res_ids': json.dumps(record_ids),
        'provider_id': provider.id,
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
        'duration': result.get('duration', 0.0),
        'batch_size': batch_size,
    }

    try:
        AiLog.sudo().create(vals)
    except Exception as exc:
        _logger.error("os_ai_fields: failed to create ai.log: %s", exc)


_logger.debug("os_ai_fields: patched odoo.fields.Field with ai_compute / ai_compute_depends / ai_compute_async / ai_compute_batch_size")
# Translation support: fields with translate=True and ai_compute automatically
# compute in all installed languages using a single multi-language LLM call.
