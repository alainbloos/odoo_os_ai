# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import base64
import json
import logging
import time

import requests as http_requests
import litellm

# Allow litellm to silently drop unsupported params (e.g. temperature for some models)
litellm.drop_params = True

from odoo import api, fields, models
from odoo.exceptions import UserError

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
}

# Cost per image for models that don't report cost via API (USD).
# Key: model name (without provider prefix). Value: cost per generated image.
IMAGE_GENERATION_COST_MAP = {
    'gpt-image-1': 0.040,                   # 1024x1024, standard quality
    'gpt-image-1.5': 0.040,                 # 1024x1024, standard quality (est.)
    'gemini-3.1-flash-image-preview': 0.067, # 1024px default
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
    ], string='Provider', required=True, default='openai')
    api_key = fields.Char('API Key')
    base_url = fields.Char(
        'Base URL',
        help="Only needed for self-hosted or custom endpoints. "
             "Leave empty for standard providers — litellm resolves the URL automatically.",
    )
    model_name = fields.Char(
        'Model Name',
        help="e.g. gpt-4o-mini, gemini-2.0-flash, claude-3-5-sonnet, deepseek-chat",
        required=True,
    )
    temperature = fields.Float('Temperature', default=0.3)
    active = fields.Boolean('Active', default=True)
    sequence = fields.Integer('Sequence', default=10)

    # Capabilities
    capable_text = fields.Boolean(
        'Text Generation', default=True,
        help="Can generate text, extract data, and use JSON schemas."
    )
    capable_vision = fields.Boolean(
        'Vision (Image to Text)', default=False,
        help="Can read and analyze images (e.g. gpt-4o, claude-3-5-sonnet)."
    )
    capable_image_generation = fields.Boolean(
        'Image Generation', default=False,
        help="Can generate new images from text prompts (e.g. dall-e-3)."
    )
    capable_image_edit = fields.Boolean(
        'Image Editing', default=False,
        help="Can take a base image and a text prompt to generate a modified image."
    )

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

            record.write({
                'capable_text': mode == 'chat',
                'capable_vision': (supports_vision and mode == 'chat') or is_native_image_model,
                'capable_image_generation': mode == 'image_generation',
                'capable_image_edit': is_native_image_model,
            })

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
                return {
                    'content': '',
                    'raw_response': {},
                    'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'cost': 0.0,
                    'model': model_id,
                    'duration': 0.0,
                    'success': False,
                    'error': "Provider '%s' cannot edit images natively. "
                             "Image editing requires a multimodal model with both "
                             "capable_image_edit and capable_vision (e.g. Gemini Flash Image)." % self.name,
                }
        elif capability_used == 'capable_image_generation':
            if self.capable_vision:
                # Native multimodal model — generate via completion API
                return self._call_native_image_generation(model_id, user_prompt)
            else:
                # Dedicated image gen model — use litellm.image_generation()
                return self._call_image_generation(model_id, user_prompt)

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
            
        if capability_used == 'capable_vision' and images:
            content_arr = [{"type": "text", "text": user_prompt}]
            for b64 in images:
                prefix = 'data:image/jpeg;base64,'
                if not b64.startswith('data:'):
                    b64 = prefix + b64
                content_arr.append({
                    "type": "image_url",
                    "image_url": {"url": b64}
                })
            messages.append({'role': 'user', 'content': content_arr})
        else:
            messages.append({'role': 'user', 'content': user_prompt})

        kwargs = {
            'model': model_id,
            'messages': messages,
            'temperature': self.temperature or 0.3,
        }

        if response_format:
            kwargs['response_format'] = response_format

        if self.api_key:
            kwargs['api_key'] = self.api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        _logger.info(
            "os_ai: calling LLM provider=%s model=%s system_len=%d prompt_len=%d",
            self.name, model_id,
            len(system_prompt) if system_prompt else 0,
            len(user_prompt),
        )

        t0 = time.time()
        try:
            response = litellm.completion(**kwargs)
            duration = time.time() - t0

            content = response.choices[0].message.content or ''
            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            # Try to get cost from litellm
            cost = 0.0
            try:
                cost = litellm.completion_cost(completion_response=response) or 0.0
            except Exception:
                pass

            # Serialize raw response
            raw = {}
            try:
                raw = response.model_dump() if hasattr(response, 'model_dump') else response.to_dict()
            except Exception:
                try:
                    raw = dict(response)
                except Exception:
                    raw = {'str': str(response)}

            return {
                'content': content.strip(),
                'raw_response': raw,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens,
                'cost': cost,
                'model': model_id,
                'duration': duration,
                'success': True,
                'error': None,
            }

        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: LLM call failed: %s", exc)
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
                'error': str(exc),
            }

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
        for b64 in (images or []):
            prefix = 'data:image/jpeg;base64,'
            if not b64.startswith('data:'):
                b64 = prefix + b64
            content_arr.append({
                "type": "image_url",
                "image_url": {"url": b64}
            })

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': content_arr})

        kwargs = {
            'model': model_id,
            'messages': messages,
            'temperature': self.temperature or 0.3,
            'modalities': ['image', 'text'],
        }
        if self.api_key:
            kwargs['api_key'] = self.api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        try:
            _logger.info(
                "os_ai: calling Native Image Edit model=%s prompt_len=%d images=%d",
                model_id, len(user_prompt), len(images or []),
            )
            response = litellm.completion(**kwargs)
            duration = time.time() - t0

            b64_img = self._extract_image_from_completion(response)

            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            cost = self._estimate_image_cost(model_id)
            try:
                api_cost = litellm.completion_cost(completion_response=response) or 0.0
                if api_cost > 0:
                    cost = api_cost
            except Exception:
                pass

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
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data found in response.",
            }

        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Native Image Edit failed: %s", exc)
            return {
                'content': '',
                'raw_response': {},
                'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'cost': 0.0,
                'model': model_id,
                'duration': duration,
                'success': False,
                'error': str(exc),
            }

    def _call_native_image_generation(self, model_id, user_prompt):
        """Image generation via multimodal completion with modalities=["image","text"].

        For multimodal models (e.g. Gemini Flash Image) that produce images
        via the standard completion API instead of a dedicated image_generation endpoint.
        """
        t0 = time.time()
        messages = [{'role': 'user', 'content': user_prompt}]

        kwargs = {
            'model': model_id,
            'messages': messages,
            'temperature': self.temperature or 0.3,
            'modalities': ['image', 'text'],
        }
        if self.api_key:
            kwargs['api_key'] = self.api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        try:
            _logger.info("os_ai: calling Native Image Generation model=%s prompt_len=%d", model_id, len(user_prompt))
            response = litellm.completion(**kwargs)
            duration = time.time() - t0

            b64_img = self._extract_image_from_completion(response)

            usage = getattr(response, 'usage', None)
            prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
            completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
            total_tokens = getattr(usage, 'total_tokens', 0) or 0

            cost = self._estimate_image_cost(model_id)
            try:
                api_cost = litellm.completion_cost(completion_response=response) or 0.0
                if api_cost > 0:
                    cost = api_cost
            except Exception:
                pass

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
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data found in response.",
            }

        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Native Image Generation failed: %s", exc)
            return {
                'content': '',
                'raw_response': {},
                'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'cost': 0.0,
                'model': model_id,
                'duration': duration,
                'success': False,
                'error': str(exc),
            }

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
                    resp_img = http_requests.get(url, timeout=30)
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
        }
        if self.api_key:
            kwargs['api_key'] = self.api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        try:
            _logger.info("os_ai: calling Image Generation model=%s prompt_len=%d", model_id, len(user_prompt))
            response = litellm.image_generation(**kwargs)
            duration = time.time() - t0
            
            b64_img = ""
            if hasattr(response, 'data') and len(response.data) > 0:
                data_obj = response.data[0]
                if hasattr(data_obj, 'b64_json') and data_obj.b64_json:
                    b64_img = data_obj.b64_json
                elif hasattr(data_obj, 'url') and data_obj.url:
                    resp_img = http_requests.get(data_obj.url, timeout=30)
                    if resp_img.status_code == 200:
                        b64_img = base64.b64encode(resp_img.content).decode('utf-8')

            # Image generation APIs don't report cost via tokens.
            # Estimate from known per-image pricing, fall back to 0.
            cost = self._estimate_image_cost(model_id)

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
                'model': model_id,
                'duration': duration,
                'success': bool(b64_img),
                'error': None if b64_img else "API returned success but no image data/url found.",
            }
            
        except Exception as exc:
            duration = time.time() - t0
            _logger.error("os_ai: Image Generation failed: %s", exc)
            return {
                'content': '',
                'raw_response': {},
                'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'cost': 0.0,
                'model': model_id,
                'duration': duration,
                'success': False,
                'error': str(exc),
            }

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
