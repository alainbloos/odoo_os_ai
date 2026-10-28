# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""
Small helpers for the ORM differences between the supported Odoo versions.
Only differences that were actually observed are handled here.
"""


def flush_all(env):
    """Flush every pending ORM write to the database."""
    if hasattr(env, 'flush_all'):       # Odoo 16+
        env.flush_all()
    else:
        env['base'].flush()


def get_param(env, key, default=''):
    """Read a system parameter (ir.config_parameter) as a string."""
    params = env['ir.config_parameter'].sudo()
    if hasattr(params, 'get_str'):      # Odoo 20+: typed getters, get_param removed
        return params.get_str(key, default)
    return params.get_param(key, default)


def set_param(env, key, value):
    """Store a system parameter (ir.config_parameter) as a string."""
    params = env['ir.config_parameter'].sudo()
    if hasattr(params, 'set_str'):      # Odoo 20+
        return params.set_str(key, value)
    return params.set_param(key, value)


def render_pdf(env, html):
    """Render an HTML document to PDF bytes with the database's report engine."""
    IrReport = env['ir.actions.report']
    if hasattr(IrReport, '_run_pdf_engine_without_processing'):  # Odoo 20+: pluggable engines
        engine = IrReport._get_pdf_engine()
        return IrReport._run_pdf_engine_without_processing(engine, [html])
    return IrReport._run_wkhtmltopdf(bodies=[html])


def clear_env(env):
    """Discard the ORM caches of the transaction, e.g. after a commit or a rollback."""
    transaction = getattr(env, 'transaction', None)
    if transaction is not None and hasattr(transaction, 'clear'):
        transaction.clear()             # env.clear() is deprecated in Odoo 20
    else:
        env.clear()
