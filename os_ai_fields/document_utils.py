# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Post-processing utilities for AI-generated documents.

These functions convert structured LLM output (JSON / HTML) into binary
documents using libraries already bundled with Odoo — no extra dependencies.

  json_to_xlsx  — JSON {"headers": [...], "rows": [[...], ...]} → base64 xlsx
  html_to_pdf   — HTML string → base64 pdf  (via wkhtmltopdf)
"""

import base64
import io
import json
import logging
import re

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excel  (openpyxl — bundled with Odoo, used by base_import)
# ---------------------------------------------------------------------------

def json_to_xlsx(content_str):
    """Convert a JSON string with *headers* + *rows* into a base64 xlsx.

    Expected JSON structure::

        {
            "headers": ["Name", "Email", "Score"],
            "rows": [
                ["Alice", "alice@example.com", 85],
                ["Bob",   "bob@example.com",   72]
            ]
        }

    Returns:
        str: base64-encoded xlsx bytes, or ``False`` on error.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        _logger.error("os_ai_fields: openpyxl is not installed — cannot generate xlsx")
        return False

    # -- Parse JSON --------------------------------------------------------
    try:
        cleaned = _strip_markdown_fences(content_str)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError) as exc:
        _logger.error("os_ai_fields: json_to_xlsx — invalid JSON: %s\n%s",
                       exc, (content_str or '')[:500])
        return False

    if not isinstance(data, dict):
        _logger.error("os_ai_fields: json_to_xlsx — expected dict, got %s", type(data).__name__)
        return False

    headers = data.get('headers') or []
    rows = data.get('rows') or []

    if not headers and not rows:
        _logger.warning("os_ai_fields: json_to_xlsx — empty headers and rows")
        return False

    # -- Build workbook ----------------------------------------------------
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    if headers:
        ws.append([str(h) for h in headers])
        # Bold header row
        from openpyxl.styles import Font
        bold = Font(bold=True)
        for cell in ws[1]:
            cell.font = bold

    for row in rows:
        if isinstance(row, list):
            ws.append([_coerce_cell_value(v) for v in row])
        else:
            ws.append([_coerce_cell_value(row)])

    # Auto-fit column widths (approximate)
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            val = str(cell.value) if cell.value is not None else ''
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(max_len + 3, 50)

    # -- Serialize to bytes ------------------------------------------------
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('ascii')


# ---------------------------------------------------------------------------
# PDF  (wkhtmltopdf — bundled with Odoo)
# ---------------------------------------------------------------------------

def html_to_pdf(env, html_str):
    """Convert an HTML string into a base64-encoded PDF via wkhtmltopdf.

    Uses Odoo's ``ir.actions.report._run_wkhtmltopdf()`` which is available
    in all supported Odoo versions (14–18).

    Args:
        env: Odoo Environment (used to access ``ir.actions.report``).
        html_str: HTML content string.

    Returns:
        str: base64-encoded PDF bytes, or ``False`` on error.
    """
    if not html_str or not html_str.strip():
        _logger.warning("os_ai_fields: html_to_pdf — empty HTML input")
        return False

    cleaned = _strip_markdown_fences(html_str)

    # Wrap in a full HTML document if the LLM returned a fragment
    if '<html' not in cleaned.lower():
        cleaned = _wrap_html_fragment(cleaned)

    try:
        IrReport = env['ir.actions.report']
        pdf_bytes = IrReport._run_wkhtmltopdf(bodies=[cleaned])
        return base64.b64encode(pdf_bytes).decode('ascii')
    except Exception as exc:
        _logger.error("os_ai_fields: html_to_pdf — wkhtmltopdf failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_cell_value(value):
    """Try to convert a string cell value to a numeric type for proper Excel formatting."""
    if not isinstance(value, str):
        return value
    # Try integer
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    # Try float
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    # Boolean-like
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    return value


def _strip_markdown_fences(text):
    """Remove markdown code fences (```json ... ```) if present."""
    if not text:
        return text
    text = text.strip()
    text = re.sub(r'^```(?:json|html|xml)?\s*\n?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n?\s*```$', '', text)
    return text.strip()


def _wrap_html_fragment(fragment):
    """Wrap an HTML fragment in a minimal HTML document for wkhtmltopdf."""
    return (
        '<!DOCTYPE html>\n'
        '<html>\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        '  <style>\n'
        '    body { font-family: Arial, Helvetica, sans-serif; font-size: 12px; margin: 20px; }\n'
        '    table { border-collapse: collapse; width: 100%%; }\n'
        '    th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }\n'
        '    th { background-color: #f5f5f5; font-weight: bold; }\n'
        '  </style>\n'
        '</head>\n'
        '<body>\n%s\n</body>\n'
        '</html>'
    ) % fragment
