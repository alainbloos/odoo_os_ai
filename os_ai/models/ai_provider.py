# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import base64
import json
import logging
import time

import requests as http_requests
import litellm

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Maps selection key -> litellm provider prefix
PROVIDER_PREFIX_MAP = {
    'openai': 'openai',
    'gemini': 'gemini',
    'anthropic': 'anthropic',
    'deepseek': 'deepseek',
    'xai': 'xai',
    'mistral': 'mistral',
    'ollama': 'ollama',
    'typesafe': 'typesafe',
}

# JSON Schema support reported by litellm, but not offered by the provider's API:
# DeepSeek documents response_format "text" and "json_object" only.
PROVIDERS_WITHOUT_JSON_SCHEMA = frozenset({'deepseek'})

# Decision models (e.g. TypeSafe Jev) answer typed questions (yes/no, one option)
# instead of generating text. They are called through litellm.systemone().
DECISION_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
DECISION_MAX_RETRIES = 2
DECISION_MAX_RETRY_WAIT = 10    # seconds

# Cost per image for models that don't report cost via API (USD).
# Key: model name (without provider prefix). Value: cost per generated image.
# Only used when litellm cannot compute the cost: logged as an estimate.
IMAGE_GENERATION_COST_MAP = {
    'gpt-image-1': 0.040,                   # 1024x1024, standard quality
    'gpt-image-1.5': 0.040,                 # 1024x1024, standard quality (est.)
    'gemini-3.1-flash-image-preview': 0.067, # 1024px default
    'gemini-3.1-flash-image': 0.067,         # 1024px default (est.)
    'grok-imagine-image': 0.020,
    'grok-imagine-image-2.0': 0.040,
}

# Models retired or deprecated by their provider, with a suggested replacement.
# Shown as a warning on the provider; the replacement is only applied when the
# user asks for it (it changes behavior and cost). The retirement date comes
# from litellm's registry when it knows it.
MODEL_REPLACEMENTS = {
    'deepseek-chat': 'deepseek-flash',               # retired 2026-07-24
    'deepseek-reasoner': 'deepseek-v4-pro',          # retired 2026-07-24
    'grok-3': 'grok-4.3',                            # retired 2026-05-15, redirected to grok-4.3
    'pixtral-large-latest': 'mistral-medium-3.5',    # deprecated 2026-02-27
    'gemini-3.1-flash-image-preview': 'gemini-3.1-flash-image',
    'gemini-2.5-flash-image': 'gemini-3.1-flash-image',
    'imagen-4.0-fast-generate-001': 'gemini-3.1-flash-image',  # Imagen 4 shut down
    'gpt-image-1': 'gpt-image-2',
    'gpt-image-1.5': 'gpt-image-2',
}

# Kind of failure reported in call_llm() results ('error_kind'):
#   retryable — temporary (rate limit, timeout, overload, network): try again later
#   permanent — retrying the same request will fail again (auth, bad request,
#               unknown model, refused content): needs a configuration change
ERROR_RETRYABLE = 'retryable'
ERROR_PERMANENT = 'permanent'

# 4xx statuses that are still temporary (timeout, conflict, rate limit)
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})

# finish_reason values that mean the answer must not be used
FINISH_REASON_ERRORS = {
    'length': ERROR_RETRYABLE,          # truncated by the output token limit
    'content_filter': ERROR_PERMANENT,  # refused / filtered by the provider
}


def classify_error(exc):
    """Return ERROR_RETRYABLE or ERROR_PERMANENT for an exception raised by litellm."""
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int) and 400 <= status < 500 and status not in RETRYABLE_STATUS_CODES:
        return ERROR_PERMANENT
    # Server errors, rate limits, timeouts, network and unknown failures:
    # retry, the caller bounds the number of attempts.
    return ERROR_RETRYABLE


def _is_unsupported_param_error(exc, param):
    """True when the provider rejected ``param`` itself (not any other 400)."""
    if type(exc).__name__ == 'UnsupportedParamsError':
        return param in str(exc)
    status = getattr(exc, 'status_code', None)
    message = str(exc).lower()
    return status == 400 and param in message and any(hint in message for hint in (
        'unsupported', 'not supported', 'does not support', 'only the default',
        'not allowed', 'is deprecated', 'not available',
    ))


# Image formats accepted by vision models, by file signature
IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def image_data_url(b64):
    """Data URL of a base64 image, with its real media type.

    Some providers (e.g. Anthropic) reject an image whose declared type does
    not match its content. Formats vision models cannot read (e.g. SVG)
    raise ValueError.
    """
    if b64.startswith('data:'):
        return b64
    head = base64.b64decode(b64[:24])
    for signature, media_type in IMAGE_SIGNATURES:
        if head.startswith(signature):
            return 'data:%s;base64,%s' % (media_type, b64)
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'data:image/webp;base64,%s' % b64
    raise ValueError('unsupported image format (only PNG, JPEG, GIF and WebP can be sent to vision models)')


class TimeBudgetExceeded(Exception):
    """The caller's deadline (e.g. the cron time budget) is over: no new call is started."""


def _retry_after(exc, default):
    """Seconds requested by the provider's Retry-After header, else default."""
    headers = getattr(exc, 'litellm_response_headers', None) or {}
    try:
        return float(headers.get('retry-after') or default)
    except (TypeError, ValueError):
        return default


def _error_result(model_id, duration, error, error_kind):
    return {
        'content': '',
        'raw_response': {},
        'prompt_tokens': 0,
        'completion_tokens': 0,
        'total_tokens': 0,
        'cost': 0.0,
        'model': model_id,
        'duration': duration,
        'success': False,
        'error': error,
        'error_kind': error_kind,
    }


class AiProvider(models.Model):
    _name = 'os.ai.provider'
    _description = 'AI Provider'
    _order = 'sequence, id'

    name = fields.Char('Name', required=True)
    provider_type = fields.Selection([
        ('openai', 'OpenAI'),
        ('gemini', 'Google Gemini'),
        ('anthropic', 'Anthropic'),
        ('deepseek', 'DeepSeek'),
        ('xai', 'xAI (Grok)'),
        ('mistral', 'Mistral'),
        ('ollama', 'Ollama (Local)'),
        ('typesafe', 'TypeSafe (Jev decisions)'),
    ], string='Provider', required=True, default='openai')
    api_key = fields.Char('API Key', groups='base.group_system')
    base_url = fields.Char(
        'Base URL',
        help="Only needed for self-hosted or custom endpoints. "
             "Leave empty for standard providers — litellm resolves the URL automatically.",
    )
    model_name = fields.Char(
        'Model Name',
        help="e.g. gpt-6-luna, claude-sonnet-5-5, gemini-3.5-flash-lite, grok-4.3, deepseek-flash",
        required=True,
    )
    temperature_mode = fields.Selection([
        ('default', 'Model default'),
        ('custom', 'Custom'),
    ], string='Temperature', required=True, default='default',
        help="Model default: the parameter is not sent. Required by models that reject it "
             "(e.g. Claude Opus 4.7+ and Claude 5, GPT-6 with reasoning) and recommended "
             "for Gemini 3.\nCustom: always send the value, even 0.",
    )
    temperature = fields.Float('Temperature Value', default=0.3)
    reasoning_effort = fields.Selection([
        ('none', 'None'),
        ('minimal', 'Minimal'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string='Reasoning Effort',
        help="Sent only when set. The accepted values depend on the model; leave empty "
             "to use the model default.",
    )
    max_output_tokens = fields.Integer(
        'Max Output Tokens',
        help="Upper bound for the answer (0 = model default). Answers cut by this limit are "
             "not used.",
    )
    timeout = fields.Integer(
        'Timeout (s)', default=180,
        help="Maximum time to wait for an answer.",
    )
    structured_output = fields.Selection([
        ('json_schema', 'JSON Schema'),
        ('json_object', 'JSON mode'),
        ('none', 'Instructions only'),
    ], string='Structured Output', required=True, default='json_schema',
        help="How field values are requested.\n"
             "JSON Schema: the provider enforces the schema (most current models).\n"
             "JSON mode: the provider only guarantees valid JSON; the schema is described in the prompt.\n"
             "Instructions only: no response_format is sent.\n"
             "Answers are always validated before being stored.",
    )
    active = fields.Boolean('Active', default=True)
    sequence = fields.Integer('Sequence', default=10)

    @api.constrains('timeout', 'max_output_tokens', 'temperature', 'temperature_mode')
    def _check_numeric_settings(self):
        for record in self:
            if record.timeout < 0:
                raise ValidationError("The timeout cannot be negative (0 = no limit).")
            if record.max_output_tokens < 0:
                raise ValidationError("Max Output Tokens cannot be negative (0 = model default).")
            if record.temperature_mode == 'custom' and not 0 <= record.temperature <= 2:
                raise ValidationError("The temperature must be between 0 and 2.")

    # Model lifecycle (retired / deprecated models)
    model_replacement = fields.Char(
        'Suggested Replacement', compute='_compute_model_status',
        help="Current model suggested by OS AI for a retired or deprecated model.",
    )
    model_status_message = fields.Char('Model Status', compute='_compute_model_status')

    # Capabilities
    capable_text = fields.Boolean(
        'Text Generation', default=True,
        help="Can generate text, extract data, and use JSON schemas."
    )
    capable_vision = fields.Boolean(
        'Vision (Image to Text)', default=False,
        help="Can read and analyze images (e.g. gpt-6-luna, claude-sonnet-5-5, gemini-3.5-flash-lite)."
    )
    capable_image_generation = fields.Boolean(
        'Image Generation', default=False,
        help="Can generate new images from text prompts (e.g. gpt-image-2, grok-imagine-image)."
    )
    capable_decision = fields.Boolean(
        'Typed Decisions', default=False,
        help="Decision model (e.g. TypeSafe Jev): answers yes/no and one-of-N questions with "
             "probabilities instead of generating text. Used only by fields that opt in with "
             "ai_compute_engine='decision'.",
    )
    capable_image_edit = fields.Boolean(
        'Image Editing', default=False,
        help="Can take a base image and a text prompt to generate a modified image."
    )

    @api.depends('provider_type', 'model_name')
    def _compute_model_status(self):
        for record in self:
            bare_model = (record.model_name or '').strip()
            prefix = PROVIDER_PREFIX_MAP.get(record.provider_type, '')
            if prefix and bare_model.startswith(prefix + '/'):
                bare_model = bare_model[len(prefix) + 1:]
            replacement = MODEL_REPLACEMENTS.get(bare_model, False)
            deprecation_date = False
            if bare_model:
                info = litellm.model_cost.get(record._build_litellm_model()) or litellm.model_cost.get(bare_model) or {}
                deprecation_date = info.get('deprecation_date')
            message = False
            if replacement or deprecation_date:
                message = "Model %s is retired or deprecated by its provider%s." % (
                    bare_model, (" (%s)" % deprecation_date) if deprecation_date else '')
                if replacement:
                    message += " Suggested replacement: %s." % replacement
            record.model_replacement = replacement
            record.model_status_message = message

    def action_apply_model_replacement(self):
        """Switch to the suggested model. Capabilities are detected again when possible."""
        for record in self.filtered('model_replacement'):
            old_model = record.model_name
            record.model_name = record.model_replacement
            _logger.info("os_ai: provider %s switched from %s to %s", record.name, old_model, record.model_name)
            try:
                record.action_detect_capabilities()
            except UserError:
                _logger.info("os_ai: capabilities of %s not found in litellm, keeping the current ones",
                             record.model_name)
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def action_detect_capabilities(self):
        """Auto-detect model capabilities by querying litellm's model registry.

        Uses litellm.get_model_info() to read:
          - mode: 'chat' vs 'image_generation'
          - supports_vision: True/False/None

        Important distinction for image models:
          Some providers (Gemini) support native image editing via the
          completion API with modalities=['image','text'].  Others (OpenAI)
          use a dedicated image_generation() endpoint that cannot accept
          input images.  litellm reports supports_vision=True for both, so
          we must filter by provider_type to avoid setting capabilities
          that would route to an unsupported code path.
        """
        # Providers whose image_generation models support native
        # image input/output via litellm.completion() + modalities.
        NATIVE_IMAGE_PROVIDERS = ('gemini',)

        for record in self:
            model_id = record._build_litellm_model()
            try:
                info = litellm.get_model_info(model_id)
            except Exception as exc:
                raise UserError(
                    "Model '%s' not found in litellm registry.\n\n"
                    "You can set the capabilities manually, or check that "
                    "the provider type and model name are correct.\n\n"
                    "Error: %s" % (model_id, exc)
                )

            mode = info.get('mode', 'chat')
            supports_vision = info.get('supports_vision') is True

            # Native multimodal image model: can receive AND produce images
            # via the completion API (e.g. Gemini Flash Image / Nano Banana).
            # Dedicated image gen models (OpenAI gpt-image-*) do NOT support
            # this — they use litellm.image_generation() instead.
            is_native_image_model = (
                mode == 'image_generation'
                and supports_vision
                and record.provider_type in NATIVE_IMAGE_PROVIDERS
            )

            vals = {
                'capable_text': mode == 'chat',
                'capable_vision': (supports_vision and mode == 'chat') or is_native_image_model,
                'capable_image_generation': mode == 'image_generation',
                'capable_image_edit': is_native_image_model,
                'capable_decision': mode == 'evaluation',
            }
            # Unknown support (None) keeps the current choice
            supports_schema = info.get('supports_response_schema')
            if supports_schema is True:
                vals['structured_output'] = (
                    'json_object' if record.provider_type in PROVIDERS_WITHOUT_JSON_SCHEMA else 'json_schema')
            elif supports_schema is False:
                vals['structured_output'] = 'none'
            record.write(vals)

            _logger.info(
                "os_ai: auto-detected capabilities for %s (%s): "
                "text=%s, vision=%s, image_gen=%s, image_edit=%s",
                record.name, model_id,
                record.capable_text, record.capable_vision,
                record.capable_image_generation, record.capable_image_edit,
            )

    def call_llm(self, system_prompt, user_prompt, response_format=None, images=None, capability_used='capable_text'):
        """Call the LLM provider and return structured result.

        Args:
            system_prompt (str): The system message (general instructions).
            user_prompt (str): The field-specific prompt.
            response_format (dict, optional): LiteLLM JSON Schema for structured output.
            images (list, optional): List of base64 strings containing image data.
            capability_used (str): 'capable_text', 'capable_vision', 'capable_image_generation', 'capable_image_edit'

        Returns:
            dict: {
                'content': str,       # assistant reply text or base64 image
                'raw_response': dict, # full litellm response as dict
                'prompt_tokens': int,
                'completion_tokens': int,
                'total_tokens': int,
                'cost': float,        # USD cost if available
                'model': str,         # model identifier used
                'duration': float,    # seconds
                'success': bool,
                'error': str or None,
                'error_kind': None, ERROR_RETRYABLE or ERROR_PERMANENT,
                'finish_reason': str or None,
            }
        """
        self.ensure_one()

        model_id = self._build_litellm_model()

        if capability_used == 'capable_image_edit':
            if self.capable_vision:
                # Native multimodal model (e.g. Gemini Flash Image) — single-step
                # edit via completion API with modalities=["image", "text"]
                return self._call_native_image_edit(model_id, system_prompt, user_prompt, images)
            else:
                _logger.error(
                    "os_ai: provider %s (%s) has capable_image_edit but not capable_vision — "
                    "native image editing requires a multimodal model that can see images",
                    self.name, self.model_name,
                )
                return _error_result(
                    model_id, 0.0,
                    "Provider '%s' cannot edit images natively. "
                    "Image editing requires a multimodal model with both "
                    "capable_image_edit and capable_vision (e.g. Gemini Flash Image)." % self.name,
                    ERROR_PERMANENT,
                )
        elif capability_used == 'capable_image_generation':
            if self.capable_vision:
                # Native multimodal model — generate via completion API
                return self._call_native_image_generation(model_id, user_prompt)
            else:
                # Dedicated image gen model — use litellm.image_generation()
                return self._call_image_generation(model_id, user_prompt)

        if response_format and self.structured_output in ('json_object', 'none'):
            # The provider does not enforce the schema (JSON mode only guarantees
            # valid JSON, instructions only nothing): describe the expected shape
            schema = response_format.get('json_schema', {}).get('schema')
            if schema:
                user_prompt = "%s\n\nRespond with a JSON object that follows this JSON schema:\n%s" % (
                    user_prompt, json.dumps(schema))
            response_format = {'type': 'json_object'} if self.structured_output == 'json_object' else None
        elif self.structured_output == 'none':
            response_format = None

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})

        if capability_used == 'capable_vision' and images:
            content_arr = [{"type": "text", "text": user_prompt}]
            try:
                for b64 in images:
                    content_arr.append({
                        "type": "image_url",
                        "image_url": {"url": image_data_url(b64)}
                    })
            except ValueError as exc:
                return _error_result(model_id, 0.0, str(exc), ERROR_PERMANENT)
            messages.append({'role': 'user', 'content': content_arr})
        else:
            messages.append({'role': 'user', 'content': user_prompt})

        kwargs = self._completion_kwargs(model_id, messages)
        if response_format:
            kwargs['response_format'] = response_format

        _logger.info(
            "os_ai: calling LLM provider=%s model=%s system_len=%d prompt_len=%d",
            self.name, model_id,
            len(system_prompt) if system_prompt else 0,
            len(user_prompt),
        )

        t0 = time.time()
        try:
            response = self._completion(kwargs)
            duration = time.time() - t0

            choice = response.choices[0]
            content = choice.message.content or ''
            finish_reason = getattr(choice, 'finish_reason', None)
            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            cost, cost_status = self._response_cost(response)

            # Serialize raw response
            raw = {}
            try:
                raw = response.model_dump() if hasattr(response, 'model_dump') else response.to_dict()
            except Exception:
                try:
                    raw = dict(response)
                except Exception:
                    raw = {'str': str(response)}

            # A truncated or refused answer is not a usable result, even if the
            # HTTP call itself succeeded.
            error_kind = FINISH_REASON_ERRORS.get(finish_reason)
            error = None
            if error_kind:
                error = "LLM answer not usable (finish_reason=%s)" % finish_reason
                _logger.warning("os_ai: %s — provider=%s model=%s", error, self.name, model_id)

            return {
                'content': content.strip(),
                'raw_response': raw,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens,
                'cost': cost,
                'cost_status': cost_status,
                'model': model_id,
                'duration': duration,
                'success': not error_kind,
                'error': error,
                'error_kind': error_kind,
                'finish_reason': finish_reason,
            }

        except Exception as exc:
            duration = time.time() - t0
            error_kind = classify_error(exc)
            _logger.error("os_ai: LLM call failed (%s): %s", error_kind, exc)
            return _error_result(model_id, duration, str(exc), error_kind)

    # ------------------------------------------------------------------
    # Private helpers — Native multimodal (Gemini Flash Image, etc.)
    # ------------------------------------------------------------------

    def _call_native_image_edit(self, model_id, system_prompt, user_prompt, images):
        """Native image editing via multimodal completion with modalities=["image","text"].

        For models like Gemini 3.1 Flash Image Preview (Nano Banana 2) that natively
        accept input images and produce edited images in a single API call —
        no need for the dual Vision→Generation pipeline.
        """
        t0 = time.time()

        # Build message content with input images + text prompt
        content_arr = [{"type": "text", "text": user_prompt}]
        try:
            for b64 in (images or []):
                content_arr.append({
                    "type": "image_url",
                    "image_url": {"url": image_data_url(b64)}
                })
        except ValueError as exc:
            return _error_result(model_id, 0.0, str(exc), ERROR_PERMANENT)

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': content_arr})

        kwargs = self._completion_kwargs(model_id, messages)
        kwargs['modalities'] = ['image', 'text']

        try:
            _logger.info(
                "os_ai: calling Native Image Edit model=%s prompt_len=%d images=%d",
                model_id, len(user_prompt), len(images or []),
            )
            response = self._completion(kwargs)
            duration = time.time() - t0

            b64_img = self._extract_image_from_completion(response)

            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            cost, cost_status = self._response_cost(response, image=True)

            raw = {}
            try:
                raw = response.model_dump() if hasattr(response, 'model_dump') else response.to_dict()
            except Exception:
                pass

            return {
                'content': b64_img,
                'raw_response': raw,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens,
                'cost': cost,
                'cost_status': cost_status,
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data found in response.",
                'error_kind': None if b64_img else ERROR_RETRYABLE,
            }

        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Native Image Edit failed: %s", exc)
            return _error_result(model_id, duration, str(exc), classify_error(exc))

    def _call_native_image_generation(self, model_id, user_prompt):
        """Image generation via multimodal completion with modalities=["image","text"].

        For multimodal models (e.g. Gemini Flash Image) that produce images
        via the standard completion API instead of a dedicated image_generation endpoint.
        """
        t0 = time.time()
        messages = [{'role': 'user', 'content': user_prompt}]

        kwargs = self._completion_kwargs(model_id, messages)
        kwargs['modalities'] = ['image', 'text']

        try:
            _logger.info("os_ai: calling Native Image Generation model=%s prompt_len=%d", model_id, len(user_prompt))
            response = self._completion(kwargs)
            duration = time.time() - t0

            b64_img = self._extract_image_from_completion(response)

            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            cost, cost_status = self._response_cost(response, image=True)

            raw = {}
            try:
                raw = response.model_dump() if hasattr(response, 'model_dump') else response.to_dict()
            except Exception:
                pass

            return {
                'content': b64_img,
                'raw_response': raw,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens,
                'cost': cost,
                'cost_status': cost_status,
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data found in response.",
                'error_kind': None if b64_img else ERROR_RETRYABLE,
            }

        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Native Image Generation failed: %s", exc)
            return _error_result(model_id, duration, str(exc), classify_error(exc))

    def _extract_image_from_completion(self, response):
        """Extract base64 image data from a litellm completion response.

        litellm returns generated images in response.choices[0].message.images
        as a list of objects: {"image_url": {"url": "data:image/png;base64,..."}}
        """
        msg = response.choices[0].message if response.choices else None
        if not msg:
            return ''

        # Primary path: message.images (litellm >= v1.77.0)
        images_list = getattr(msg, 'images', None)
        if images_list and len(images_list) > 0:
            img_obj = images_list[0]
            url = ''
            if isinstance(img_obj, dict):
                url = img_obj.get('image_url', {}).get('url', '')
            elif hasattr(img_obj, 'image_url'):
                url = getattr(img_obj.image_url, 'url', '') or ''

            if url.startswith('data:'):
                # Strip the "data:image/...;base64," prefix
                _, _, b64_data = url.partition(',')
                return b64_data
            elif url:
                # It's a plain URL — download and encode
                try:
                    resp_img = http_requests.get(url, timeout=self._download_timeout())
                    if resp_img.status_code == 200:
                        return base64.b64encode(resp_img.content).decode('utf-8')
                except Exception as dl_exc:
                    _logger.warning("os_ai: Failed to download generated image URL: %s", dl_exc)

        # Fallback: check message.image (older litellm versions)
        image_attr = getattr(msg, 'image', None)
        if image_attr:
            if isinstance(image_attr, str):
                if image_attr.startswith('data:'):
                    _, _, b64_data = image_attr.partition(',')
                    return b64_data
                return image_attr

        # Last resort: check content for inline base64 image data
        content = getattr(msg, 'content', '') or ''
        if content and len(content) > 500 and not content.strip().startswith('{'):
            # Might be raw base64 — validate
            try:
                base64.b64decode(content[:100])
                return content
            except Exception:
                pass

        return ''

    def _estimate_image_cost(self, model_id):
        """Estimate per-image cost from the cost map."""
        bare_model = (self.model_name or '').strip()
        return IMAGE_GENERATION_COST_MAP.get(bare_model, 0.0)

    # ------------------------------------------------------------------
    # Private helpers — Dedicated image generation (DALL-E, gpt-image-1)
    # ------------------------------------------------------------------

    def _call_image_generation(self, model_id, user_prompt):
        """Call litellm.image_generation, returning base64"""
        t0 = time.time()
        kwargs = {
            'model': model_id,
            'prompt': user_prompt,
            'response_format': 'b64_json',
            'drop_params': True,
        }
        if self._call_timeout():
            kwargs['timeout'] = self._call_timeout()
        if self._time_left() is not None:
            kwargs['max_retries'] = 0  # no hidden SDK retry after the deadline: the caller retries
        api_key = self._get_api_key()
        if api_key:
            kwargs['api_key'] = api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        try:
            _logger.info("os_ai: calling Image Generation model=%s prompt_len=%d", model_id, len(user_prompt))
            self._check_time_left()
            response = litellm.image_generation(**kwargs)
            duration = time.time() - t0
            
            b64_img = ""
            if hasattr(response, 'data') and len(response.data) > 0:
                data_obj = response.data[0]
                if hasattr(data_obj, 'b64_json') and data_obj.b64_json:
                    b64_img = data_obj.b64_json
                elif hasattr(data_obj, 'url') and data_obj.url:
                    resp_img = http_requests.get(data_obj.url, timeout=self._download_timeout())
                    if resp_img.status_code == 200:
                        b64_img = base64.b64encode(resp_img.content).decode('utf-8')

            cost, cost_status = self._response_cost(response, image=True)

            raw = {}
            try:
                raw = response.model_dump() if hasattr(response, 'model_dump') else response.to_dict()
            except Exception:
                pass

            return {
                'content': b64_img,
                'raw_response': raw,
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
                'cost': cost,
                'cost_status': cost_status,
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data/url found.",
                'error_kind': None if b64_img else ERROR_RETRYABLE,
            }
            
        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Image Generation failed: %s", exc)
            return _error_result(model_id, duration, str(exc), classify_error(exc))

    def call_decision(self, state, questions):
        """Ask typed questions to a decision model (e.g. TypeSafe Jev) with litellm.systemone().

        Args:
            state (dict|str): the data the questions are about.
            questions (dict): {question_id: {"type": "noul"|"choice"|"score",
                "instructions": ..., "criteria": ...}}

        Returns:
            dict: same keys as call_llm() (content is the JSON of the answers),
            plus 'answers': {question_id: answer dict}. A response that does not
            answer every question with the requested type is not a success.
        """
        self.ensure_one()
        model_id = self._build_litellm_model()
        kwargs = {'model': model_id, 'state': state, 'questions': questions}
        if self._call_timeout():
            kwargs['timeout'] = self._call_timeout()
        api_key = self._get_api_key()
        if api_key:
            kwargs['api_key'] = api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        _logger.info("os_ai: calling decision model provider=%s model=%s questions=%d",
                     self.name, model_id, len(questions))
        t0 = time.time()
        attempt = 0
        while True:
            try:
                self._check_time_left()
                response = litellm.systemone(**kwargs)
                break
            except Exception as exc:
                # litellm does not retry decision calls: a short, bounded retry when the
                # provider answers with a rate limit or overload (connection errors carry a
                # synthesized status and are left to the cron, like any other failure)
                wait = min(_retry_after(exc, 2 ** (attempt + 1)), DECISION_MAX_RETRY_WAIT)
                deadline = self.env.context.get('os_ai_deadline')
                if (getattr(exc, 'status_code', None) in DECISION_RETRY_STATUS_CODES
                        and not isinstance(exc, litellm.APIConnectionError) and attempt < DECISION_MAX_RETRIES
                        and not (deadline and time.monotonic() + wait >= deadline)):
                    attempt += 1
                    time.sleep(wait)
                    if self._call_timeout():
                        kwargs['timeout'] = self._call_timeout()
                    continue
                kind = classify_error(exc)
                _logger.error("os_ai: decision call failed (%s): %s", kind, exc)
                return _error_result(model_id, time.time() - t0, str(exc), kind)

        duration = time.time() - t0
        usage = response.usage
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0
        cost, cost_status = self._response_cost(response)
        consumed = {
            'raw_response': response.model_dump(),
            'prompt_tokens': input_tokens,
            'completion_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'cost': cost,
            'cost_status': cost_status,
        }
        try:
            # litellm leaves out answers whose type does not match the question
            answers = {key: answer.model_dump() for key, answer in response.answers.items()}
            for question_id, question in questions.items():
                if (answers.get(question_id) or {}).get('type') != question['type']:
                    raise ValueError("no %s answer for question '%s'" % (question['type'], question_id))
        except Exception as exc:
            error = "Invalid decision response: %s" % exc
            _logger.error("os_ai: %s", error)
            return dict(_error_result(model_id, duration, error, ERROR_RETRYABLE), **consumed)

        return dict(consumed, **{
            'content': json.dumps(answers),
            'answers': answers,
            'model': response.model or self.model_name,
            'duration': duration,
            'success': True,
            'error': None,
            'error_kind': None,
        })

    def _time_left(self):
        """Seconds before the caller's deadline (context key ``os_ai_deadline``, a
        time.monotonic() value, e.g. the end of the cron time budget); None without one."""
        deadline = self.env.context.get('os_ai_deadline')
        return deadline - time.monotonic() if deadline else None

    def _check_time_left(self):
        """Refuse to start a call (or a retry, or a download) once the deadline is over."""
        left = self._time_left()
        if left is not None and left <= 0:
            raise TimeBudgetExceeded("the time budget of the caller is over: call not started")

    def _call_timeout(self):
        """Seconds a call may take: the provider timeout, shortened to the time left.
        None means no limit; 0 means the deadline is over (see _check_time_left)."""
        timeout = self.timeout if self.timeout > 0 else None
        left = self._time_left()
        if left is not None:
            left = max(left, 0.0)
            timeout = min(timeout, left) if timeout else left
        return timeout

    def _download_timeout(self):
        """Timeout of the download of a generated image: 30 s, or the time left."""
        self._check_time_left()
        left = self._time_left()
        return 30 if left is None else min(30, left)

    def _get_api_key(self):
        """The API key is restricted to administrators; LLM calls may run as any user."""
        self.ensure_one()
        return self.sudo().api_key

    def _completion_kwargs(self, model_id, messages):
        """litellm.completion() arguments shared by every chat call of this provider.

        Optional parameters are only sent when configured: several current models
        reject values they do not support (e.g. temperature).
        """
        self.ensure_one()
        kwargs = {
            'model': model_id,
            'messages': messages,
            # drop parameters a model does not support (e.g. temperature), for
            # these calls only: litellm's global setting belongs to the process
            'drop_params': True,
        }
        if self.temperature_mode == 'custom':
            kwargs['temperature'] = self.temperature
        if self.reasoning_effort:
            kwargs['reasoning_effort'] = self.reasoning_effort
            if self.provider_type == 'deepseek' and self.reasoning_effort != 'none':
                # litellm turns reasoning_effort into thinking on/off for DeepSeek and drops
                # the level; the API reads it from the top-level reasoning_effort field.
                kwargs['extra_body'] = {'reasoning_effort': self.reasoning_effort}
        if self.max_output_tokens > 0:
            kwargs['max_tokens'] = self.max_output_tokens
        if self._call_timeout():
            kwargs['timeout'] = self._call_timeout()
        if self._time_left() is not None:
            kwargs['max_retries'] = 0  # no hidden SDK retry after the deadline: the caller retries
        api_key = self._get_api_key()
        if api_key:
            kwargs['api_key'] = api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url
        return kwargs

    def _completion(self, kwargs):
        """Call litellm.completion(); retry once without temperature if the model rejects it.

        No call starts after the caller's deadline (TimeBudgetExceeded)."""
        self._check_time_left()
        try:
            return litellm.completion(**kwargs)
        except Exception as exc:
            if 'temperature' not in kwargs or not _is_unsupported_param_error(exc, 'temperature'):
                raise
            _logger.warning(
                "os_ai: model %s rejected the custom temperature of provider %s, retrying without it. "
                "Set its Temperature to 'Model default' to avoid this extra call. Error: %s",
                kwargs.get('model'), self.name, exc,
            )
            self._check_time_left()
            retry = {k: v for k, v in kwargs.items() if k != 'temperature'}
            if self._call_timeout():
                retry['timeout'] = self._call_timeout()
            return litellm.completion(**retry)

    def _response_cost(self, response, image=False):
        """Return (cost, cost_status): 'reported' by litellm, 'estimated' or 'unknown'."""
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            cost = None
        if cost:
            return cost, 'reported'
        if image:
            estimate = self._estimate_image_cost(None)
            if estimate:
                return estimate, 'estimated'
        if cost == 0 and not image:
            return 0.0, 'reported'
        return 0.0, 'unknown'

    def _build_litellm_model(self):
        """Build the litellm model string based on provider_type."""
        self.ensure_one()
        model = self.model_name or ''
        prefix = PROVIDER_PREFIX_MAP.get(self.provider_type, '')

        if prefix:
            prefix_slash = prefix + '/'
            if not model.startswith(prefix_slash):
                model = prefix_slash + model

        return model
