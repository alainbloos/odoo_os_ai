# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).


def migrate(cr, version):
    # Only when coming from a module version before 1.1.0: a database already
    # at 1.1.0 of the previous Odoo series must keep the users' choices.
    if not version or tuple(int(part) for part in version.split('.')[-3:]) >= (1, 1, 0):
        return
    # Providers created before temperature_mode existed always sent their
    # temperature: keep that behavior. Switching to "Model default" (required
    # by several current models) is left to the user.
    cr.execute("UPDATE os_ai_provider SET temperature_mode = 'custom'")
