# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import new_test_user

from odoo.addons.os_ai_fields import patch as ai_patch

from .common import AiFieldsCase, llm_ok


@tagged('post_install', '-at_install')
class TestRelationalCandidates(AiFieldsCase):
    """Relational answers are always validated against the authorized candidates."""

    def setUp(self):
        super().setUp()
        self.partner = self.Partner.create({'name': 'Relational partner', 'city': 'Rosario'})
        self.reset_pending()
        self.set_sql(self.partner, ai_suggested_country=None, ai_suggested_country_pending=True)
        self.argentina = self.env.ref('base.ar')
        self.france = self.env.ref('base.fr')
        self.field = self.Partner._fields['ai_suggested_country']

    def answer(self, value):
        return lambda prompt, fmt: llm_ok(json.dumps({'value': value}))

    def country(self):
        return self.sql_values(self.partner, 'ai_suggested_country', 'ai_suggested_country_pending')[self.partner.id]

    def test_valid_candidate_is_assigned(self):
        with self.mock_llm(self.answer(self.argentina.id)):
            self.run_cron()
        self.assertEqual(self.country(), (self.argentina.id, False))

    def test_candidate_outside_domain_is_rejected(self):
        with self.mock_llm(self.answer(self.france.id)):
            self.run_cron()
        self.assertEqual(self.country(), (None, True))

    def test_no_candidates_never_accepts_an_id(self):
        with patch.object(self.field, 'domain', [('code', '=', 'XX')]), \
                self.mock_llm(self.answer(self.argentina.id)):
            self.run_cron()
        self.assertNotEqual(self.country()[0], self.argentina.id)

    def test_failing_domain_is_closed(self):
        def broken_domain(records):
            raise ValueError('broken domain')

        with patch.object(self.field, 'domain', broken_domain), \
                self.mock_llm(self.answer(self.france.id)):
            self.run_cron()
        self.assertEqual(self.country(), (None, True))


@tagged('post_install', '-at_install')
class TestCompanyCandidates(AiFieldsCase):
    """check_company: candidates are shared records plus those of the record's company."""

    def setUp(self):
        super().setUp()
        Company = self.env['res.company']
        self.company_a = Company.create({'name': 'Company A (candidates test)'})
        self.company_b = Company.create({'name': 'Company B (candidates test)'})
        self.shared = self.Partner.create({'name': 'CoCand shared', 'company_id': False})
        self.only_a = self.Partner.create({'name': 'CoCand of A', 'company_id': self.company_a.id})
        self.only_b = self.Partner.create({'name': 'CoCand of B', 'company_id': self.company_b.id})
        self.field = self.Partner._fields['parent_id']

    def candidates(self, record):
        with patch.object(self.field, 'check_company', True), \
                patch.object(self.field, 'domain', [('name', '=like', 'CoCand %')]):
            result = ai_patch.get_relational_candidates(self.env, self.field, record)
        self.assertEqual(result.status, 'ok')
        return set(result.ids)

    def test_candidates_follow_the_record_company(self):
        record_a = self.Partner.create({'name': 'Record A', 'company_id': self.company_a.id})
        record_b = self.Partner.create({'name': 'Record B', 'company_id': self.company_b.id})
        self.assertEqual(self.candidates(record_a), {self.shared.id, self.only_a.id})
        self.assertEqual(self.candidates(record_b), {self.shared.id, self.only_b.id})

    def test_rule_of_the_comodel(self):
        """Odoo 18+ applies the comodel's own company rule (e.g. company_ids for accounts),
        also when it has no company_id; older versions only comodels with company_id."""
        from odoo import release
        Tag = self.env['res.partner.category']
        allowed = Tag.create({'name': 'CoRule allowed'})
        other = Tag.create({'name': 'CoRule other'})
        field = self.Partner._fields['category_id']
        record = self.Partner.create({'name': 'Rule record', 'company_id': self.company_a.id})
        seen = []

        def rule(model, companies):
            seen.append(companies)
            return [('id', '=', allowed.id)]

        with patch.object(type(Tag), '_check_company_domain', rule, create=True), \
                patch.object(field, 'check_company', True), \
                patch.object(field, 'domain', [('name', '=like', 'CoRule %')]):
            depends = ai_patch._candidates_depend_on_company(self.env, field)
            result = ai_patch.get_relational_candidates(self.env, field, record)
        if release.version_info[0] >= 18:
            self.assertTrue(depends)
            self.assertEqual(set(result.ids), {allowed.id})
            self.assertEqual(seen, [self.company_a])
        else:
            self.assertFalse(depends)
            self.assertEqual(set(result.ids), {allowed.id, other.id})

    def test_users_of_several_companies(self):
        """A user working for the record's company qualifies, as in Odoo."""
        user = new_test_user(self.env, login='os_ai_multi_company_user', groups='base.group_user',
                             company_id=self.company_a.id, company_ids=[(6, 0, (self.company_a | self.company_b).ids)])
        record = self.Partner.create({'name': 'Record of B', 'company_id': self.company_b.id})
        field = self.Partner._fields['user_id']
        with patch.object(field, 'check_company', True), patch.object(field, 'domain', [('id', '=', user.id)]):
            result = ai_patch.get_relational_candidates(self.env, field, record)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(set(result.ids), {user.id})

    def test_record_without_company_only_gets_shared_candidates(self):
        record = self.Partner.create({'name': 'Record shared', 'company_id': False})
        self.assertEqual(self.candidates(record), {self.shared.id})

    def test_company_dependent_candidates_are_computed_record_by_record(self):
        # parent_id made to look like an AI field (they are computed as superuser)
        with patch.object(self.field, 'ai_compute', 'Pick the parent company of {name}'), \
                patch.object(self.field, 'compute_sudo', True), \
                patch.object(self.field, 'domain', [('name', '=like', 'CoCand %')]):
            with patch.object(self.field, 'check_company', True):
                plan, _reason = ai_patch.prepare_computation(self.env, 'res.partner', 'parent_id', self.field, 40)
                self.assertEqual(plan['batch_size'], 1)
            with patch.object(self.field, 'check_company', False):
                plan, _reason = ai_patch.prepare_computation(self.env, 'res.partner', 'parent_id', self.field, 40)
                self.assertEqual(plan['batch_size'], 40)


@tagged('post_install', '-at_install')
class TestCandidateAccess(AiFieldsCase):
    """Candidates follow the access rules of the computing user unless computed as superuser."""

    def setUp(self):
        super().setUp()
        Tag = self.env['res.partner.category']
        self.visible = Tag.create({'name': 'Access visible tag'})
        self.hidden = Tag.create({'name': 'Access hidden tag'})
        model_id = self.env['ir.model']._get_id('res.partner.category')
        domain = "[('name', '!=', 'Access hidden tag')]"
        if 'ir.rule' in self.env:
            self.env['ir.rule'].create({'name': 'Hide a tag (test)', 'model_id': model_id, 'domain_force': domain})
        else:  # Odoo 20+: a record rule is an ir.access restriction (no group)
            self.env['ir.access'].create({'name': 'Hide a tag (test)', 'model_id': model_id, 'operation': 'r',
                                         'domain': domain})
        self.user = new_test_user(self.env, login='os_ai_candidates_user', groups='base.group_user,base.group_partner_manager')
        self.field = self.Partner._fields['ai_suggested_tags']
        self.record = self.Partner.create({'name': 'Access record'})

    def candidates(self, compute_sudo):
        env = self.env(user=self.user)
        with patch.object(self.field, 'compute_sudo', compute_sudo), \
                patch.object(self.field, 'domain', [('name', '=like', 'Access %')]):
            result = ai_patch.get_relational_candidates(env, self.field, self.record.with_env(env))
        return set(result.ids)

    def test_user_rules_apply_without_sudo(self):
        self.assertEqual(self.candidates(False), {self.visible.id})

    def test_superuser_computation_sees_every_candidate(self):
        self.assertEqual(self.candidates(True), {self.visible.id, self.hidden.id})

    def test_cron_cannot_compute_without_sudo(self):
        with patch.object(self.field, 'compute_sudo', False):
            plan, reason = ai_patch.prepare_computation(self.env, 'res.partner', 'ai_suggested_tags', self.field, 40)
        self.assertIsNone(plan)
        self.assertIn('compute_sudo=False', reason)
