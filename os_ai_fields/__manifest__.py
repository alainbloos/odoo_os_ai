# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'OS AI Computed Fields',
    'version': '19.0.1.0.0',
    'category': 'Customizations',
    'summary': 'Add ai_compute parameter to any Odoo field for LLM-powered values',
    'description': """
        Adds ai_compute, ai_compute_depends, ai_compute_async and ai_compute_batch_size
        parameters to all Odoo fields. Evaluates prompts against litellm models.
    """,
    'author': 'Alain Bloos',
    'website': 'https://github.com/alainbloos/odoo_os_ai',
    'depends': ['os_ai'],
    'data': [
        'data/ai_cron.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'images': ['static/description/icon.png'],
    'license': 'LGPL-3',
}
