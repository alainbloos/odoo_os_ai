# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    'name': 'AI Fields Demo',
    'version': '16.0.1.0.0',
    'summary': 'Demo module showcasing ai_compute on res.partner with various field types',
    'category': 'Technical',
    'author': 'Alain Bloos',
    'maintainer': 'Alain Bloos',
    'website': 'https://github.com/alainbloos/odoo_os_ai',
    'depends': ['os_ai_fields', 'contacts'],
    'data': [
        'data/ai_provider_demo.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
