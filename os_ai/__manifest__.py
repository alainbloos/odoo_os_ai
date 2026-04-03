# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'OS AI Base',
    'version': '17.0.1.0.0',
    'category': 'Technical',
    'summary': 'AI provider management, capability routing, and logging via litellm',
    'description': """
        Base module to configure AI Providers (OpenAI, Gemini, Anthropic, etc),
        track logs (prompts, responses, tokens, cost), and manage reusable prompt templates.

        Requires `litellm` Python package.
    """,
    'author': 'Alain Bloos',
    'website': 'https://github.com/alainbloos/odoo_os_ai',
    'depends': ['base'],
    'external_dependencies': {
        'python': ['litellm'],
    },
    'data': [
        'security/ir.model.access.csv',
        'views/ai_provider_views.xml',
        'views/ai_prompt_views.xml',
        'views/ai_log_views.xml',
    ],
    'installable': True,
    'application': True,
    'images': ['static/description/icon.png'],
    'license': 'LGPL-3',
}
