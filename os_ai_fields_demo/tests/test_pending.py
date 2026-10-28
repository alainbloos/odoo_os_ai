# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo.tests import tagged

from .common import AiFieldsCase


@tagged('post_install', '-at_install')
class TestPendingFlag(AiFieldsCase):
    """The async compute must persist {field}_pending, not only cache it."""

    def test_pending_persisted_on_create(self):
        partner = self.Partner.create({'name': 'Pending on create'})
        self.assertEqual(self.sql_values(partner, 'ai_greeting_pending')[partner.id], (True,))

    def test_pending_persisted_on_dependency_write(self):
        partner = self.Partner.create({'name': 'Pending on write'})
        self.set_sql(partner, ai_greeting_pending=False)
        partner.write({'name': 'Pending on write (renamed)'})
        self.assertEqual(self.sql_values(partner, 'ai_greeting_pending')[partner.id], (True,))

    def test_pending_untouched_by_unrelated_write(self):
        partner = self.Partner.create({'name': 'Unrelated write'})
        self.set_sql(partner, ai_greeting_pending=False)
        partner.write({'ref': 'REF-001'})  # not a dependency of ai_greeting
        self.assertEqual(self.sql_values(partner, 'ai_greeting_pending')[partner.id], (False,))

    def test_dependency_change_touches_already_pending_row(self):
        """The row is updated even when the flag is already set.

        Otherwise a cron computing the record meanwhile from the previous inputs
        would not get a serialization conflict and would store a stale answer.
        """
        parent = self.Partner.create({'name': 'Already pending parent', 'is_company': True})
        self.set_sql(parent, ai_contacts_summary_pending=True)
        self.assertTrue(parent.ai_contacts_summary_pending)  # cached: the ORM sees no change below
        ctid_before = self.sql_values(parent, 'ctid')[parent.id]
        self.Partner.create({'name': 'New contact', 'parent_id': parent.id})  # child_ids is a dependency
        self.assertNotEqual(self.sql_values(parent, 'ctid')[parent.id], ctid_before)
        self.assertEqual(self.sql_values(parent, 'ai_contacts_summary_pending')[parent.id], (True,))

    def test_renaming_the_parent_computes_again(self):
        """The demo prompts read related names: they are dependencies too."""
        parent = self.Partner.create({'name': 'Old parent name', 'is_company': True})
        child = self.Partner.create({'name': 'Bio child', 'parent_id': parent.id})
        self.set_sql(child, ai_bio_pending=False)
        parent.write({'name': 'New parent name'})
        self.assertEqual(self.sql_values(child, 'ai_bio_pending')[child.id], (True,))

    def test_renamed_related_values_compute_again(self):
        """Names read through a relation are dependencies too."""
        parent = self.Partner.create({'name': 'Summary parent', 'is_company': True})
        child = self.Partner.create({'name': 'Child old name', 'parent_id': parent.id})
        tag = self.env['res.partner.category'].create({'name': 'Tag old name'})
        Bank = self.env['res.partner.bank']
        number = 'account_number' if 'account_number' in Bank._fields else 'acc_number'  # Odoo 20 renamed it
        bank = Bank.create({'partner_id': parent.id, number: '0012345678'})
        parent.write({'category_id': [(4, tag.id)]})
        for change in (lambda: child.write({'name': 'Child new name'}),
                       lambda: tag.write({'name': 'Tag new name'})):
            self.set_sql(parent, ai_contacts_summary_pending=False)
            change()
            self.assertEqual(self.sql_values(parent, 'ai_contacts_summary_pending')[parent.id], (True,))
        self.set_sql(parent, ai_bank_summary_pending=False)
        bank.write({number: '0098765432'})
        self.assertEqual(self.sql_values(parent, 'ai_bank_summary_pending')[parent.id], (True,))

    def test_new_record_does_not_crash(self):
        """Onchange records (NewId) must not raise 'Compute method failed to assign'."""
        record = self.Partner.new({'name': 'Onchange record'})
        self.assertFalse(record.ai_greeting)
        self.assertFalse(record.ai_profile_score)
        self.assertFalse(record.ai_suggested_country)
