# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Utility to resolve prompt templates into concrete strings by navigating
record field values with dot-notation support.
"""

import re
from datetime import date


# Match {token} but not {{ escaped }}
_PLACEHOLDER_RE = re.compile(r'\{([^{}]+)\}')


def resolve_prompt(prompt_text, record):
    """Resolve a prompt template against an Odoo record.

    Placeholders like ``{name}``, ``{partner_id.name}``, or
    ``{partner_id.country_id.code}`` are replaced with the record's values.

    Special variables:
        * ``{__date__}``    → current date (YYYY-MM-DD)
        * ``{__user__}``    → ``record.env.user.name``
        * ``{__company__}`` → ``record.env.company.name``

    Args:
        prompt_text (str): The template string.
        record: A single Odoo record (recordset of length 1).

    Returns:
        tuple: (resolved_prompt: str, extracted_images: list of str)
               Where extracted_images contains base64 strings of any
               binary fields found in the placeholders.
    """
    extracted_images = []

    def _replace(match):
        token = match.group(1).strip()

        # --- system variables ---
        if token == '__date__':
            return str(date.today())
        if token == '__user__':
            try:
                return record.env.user.name or ''
            except Exception:
                return ''
        if token == '__company__':
            try:
                return record.env.company.name or ''
            except Exception:
                return ''

        # --- dot-notation traversal ---
        try:
            value = record
            for part in token.split('.'):
                value = getattr(value, part, None)
                if value is None:
                    return ''
            # If the final value is a recordset, use display_name
            if hasattr(value, '_name') and hasattr(value, 'display_name'):
                if len(value) > 1:
                    return ', '.join(filter(None, value.mapped('display_name')))
                return value.display_name or ''
            
            # Check if it's a binary field (bytes in Python 3 Odoo)
            if isinstance(value, bytes):
                # Likely an image/binary. Extract the base64 string
                base64_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                extracted_images.append(base64_str)
                return '[IMAGE]'

            if value is None or value is False:
                return ''
            return str(value)
        except Exception:
            return ''

    resolved_text = _PLACEHOLDER_RE.sub(_replace, prompt_text or '')
    return resolved_text, extracted_images
