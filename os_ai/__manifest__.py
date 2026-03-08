# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'OS AI Base',
    'version': '18.0.1.0.0',
    'category': 'Hidden',
    'summary': 'Core AI Provider definitions and logging. Depends on litellm',
    'description': """
        Base module to configure AI Providers (OpenAI, Gemini, Anthropic, etc),
        track logs (prompts, responses, tokens, cost), and manage reusable prompt templates.
        
        Requires `litellm` Python package.
    """,
    'author': 'Alain Bloos',
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
    'application': False,
    'license': 'LGPL-3',
}
