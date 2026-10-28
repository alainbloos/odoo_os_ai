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
# {field} or {relation.field}; other text between braces (e.g. JSON) is literal
_FIELD_PATH_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')


def record_company(record):
    """Company the record belongs to, when it is unambiguous.

    Records without a company field, or shared between companies
    (company_id empty), use the company of the current environment.
    """
    if 'company_id' in record._fields:
        company = record.company_id
        if company and company._name == 'res.company' and len(company) == 1:
            return company
    return record.env.company


def resolve_prompt(prompt_text, record):
    """Resolve a prompt template against an Odoo record.

    Placeholders like ``{name}``, ``{partner_id.name}``, or
    ``{partner_id.country_id.code}`` are replaced with the record's values.

    Special variables:
        * ``{__date__}``    → current date (YYYY-MM-DD)
        * ``{__user__}``    → ``record.env.user.name``
        * ``{__company__}`` → name of the record's company (see ``record_company``)

    Args:
        prompt_text (str): The template string.
        record: A single Odoo record (recordset of length 1).

    An empty value (False, empty relation) gives an empty string; text between
    braces that is not a field path (e.g. a JSON example) is kept as is.

    Returns:
        tuple: (resolved_prompt: str, extracted_images: list of str)
               Where extracted_images contains base64 strings of any
               binary fields found in the placeholders.

    Raises:
        ValueError: a placeholder names no field of the record, or its value
            cannot be read (e.g. access rights): the prompt would miss data.
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
                return record_company(record).name or ''
            except Exception:
                return ''

        # --- dot-notation traversal ---
        if not _FIELD_PATH_RE.fullmatch(token):
            return match.group(0)
        try:
            value = record
            field = None
            for part in token.split('.'):
                if not hasattr(value, '_fields'):
                    raise ValueError('invalid placeholder {%s}: %s is not a relation' % (token, part))
                if part not in value._fields:
                    raise ValueError('invalid placeholder {%s}: %s has no field %s' % (token, value._name, part))
                if not value:
                    return ''  # empty relation
                field = value._fields[part]
                value = value[part] if len(value) == 1 else value.mapped(part)
            if field is not None and field.type == 'boolean':
                # False is a value, not a missing one
                return ', '.join(str(bool(item)) for item in value) if isinstance(value, list) else str(bool(value))
            # If the final value is a recordset, use display_name
            if hasattr(value, '_name') and hasattr(value, 'display_name'):
                if len(value) > 1:
                    return ', '.join(filter(None, value.mapped('display_name')))
                return value.display_name or ''
            if isinstance(value, list):  # a field of several related records; 0 is a value
                return ', '.join(str(item) for item in value if item is not None and item is not False and item != '')
            
            # Check if it's a binary field (bytes in Python 3 Odoo)
            if isinstance(value, bytes):
                # Likely an image/binary. Extract the base64 string
                base64_str = value.decode('utf-8') if isinstance(value, bytes) else str(value)
                extracted_images.append(base64_str)
                return '[IMAGE]'

            if value is None or value is False:
                return ''
            return str(value)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('cannot read {%s}: %s' % (token, exc)) from None

    resolved_text = _PLACEHOLDER_RE.sub(_replace, prompt_text or '')
    return resolved_text, extracted_images
