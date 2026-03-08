# -*- coding: utf-8 -*-
# Copyright 2026 Alain Bloos <alainbloos@gmail.com>
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # ==================================================================
    # AI FIELDS DEMO
    #
    # Each field below demonstrates specific ai_compute parameters.
    # Available parameters for any field:
    #
    #   ai_compute            (str)  — prompt template with {field} placeholders
    #   ai_compute_depends    (list) — dependency fields (triggers recompute)
    #   ai_compute_async      (bool) — True: cron-based (default)
    #                                  False: compute blocking
    #   ai_compute_batch_size (int)  — max records per LLM call (default: 40)
    #
    # When the field also has translate=True, the system automatically
    # generates the value in ALL installed languages in a single LLM call.
    #
    # When the field is relational (Many2one/Many2many), the system uses
    # the field's native `domain` to provide available options to the LLM.
    # ==================================================================

    # ------------------------------------------------------------------
    # Text + translate — multi-language AI bio
    # Demonstrates: translate=True (auto generates in all installed langs)
    # ------------------------------------------------------------------
    ai_bio = fields.Text(
        "AI Bio",
        translate=True,                          # ← auto computes in all installed languages
        ai_compute="Write a short professional bio (2-3 sentences) for the contact "
                   "{name} from company {parent_id.name} located in {city}, {country_id.name}. "
                   "Date: {__date__}",
        ai_compute_depends=['name', 'parent_id', 'city', 'country_id'],
        # ai_compute_async=True,                 # ← default: True (cron-based)
        # ai_compute_batch_size=40,              # ← default: 40 records per LLM call
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Char — system variables __user__ and __company__
    # Demonstrates: ai_compute_batch_size=20 (smaller batches)
    # ------------------------------------------------------------------
    ai_greeting = fields.Char(
        "AI Greeting",
        ai_compute="Generate a short personalized greeting (1 sentence) for {name}. "
                   "The current user is {__user__} from company {__company__}.",
        ai_compute_depends=['name'],
        ai_compute_batch_size=20,                # ← smaller batch for shorter prompts
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Integer — numeric field parsing
    # Demonstrates: numeric response parsing (LLM → integer)
    # ------------------------------------------------------------------
    ai_profile_score = fields.Integer(
        "AI Profile Score",
        ai_compute="Evaluate how complete this contact's profile is, "
                   "from 0 to 100. Available data: name={name}, "
                   "email={email}, phone={phone}, mobile={mobile}, "
                   "website={website}, is_company={is_company}, "
                   "city={city}, country={country_id.name}, "
                   "job_title={function}. "
                   "Respond ONLY with an integer number.",
        ai_compute_depends=['name', 'email', 'phone', 'mobile', 'website',
                            'is_company', 'city', 'country_id', 'function'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Float — decimal field parsing
    # Demonstrates: Many2one dot-notation (state_id.name), Selection (lang)
    # ------------------------------------------------------------------
    ai_engagement_score = fields.Float(
        "AI Engagement Score",
        ai_compute="Estimate an engagement score from 0.0 to 10.0 "
                   "for the contact {name}, who speaks language={lang}, "
                   "is in {state_id.name}, {country_id.name}, "
                   "has email={email}, phone={phone}. "
                   "Respond ONLY with a decimal number (e.g. 7.5).",
        ai_compute_depends=['name', 'lang', 'state_id', 'country_id', 'email', 'phone'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Html — complex prompt with many dependencies
    # Demonstrates: Html field, many ai_compute_depends, using comment (notes)
    # ------------------------------------------------------------------
    ai_profile_card = fields.Html(
        "AI Profile Card",
        ai_compute="Generate a simple HTML business card "
                   "(using <b>, <i>, <br>, <ul>, <li>) for {name}. "
                   "Title: {title.name}. Company: {parent_id.name}. "
                   "Job title: {function}. Email: {email}. Phone: {phone}. "
                   "Address: {street}, {city}, {state_id.name}, "
                   "{country_id.name} {zip}. "
                   "Internal notes: {comment}. "
                   "Do not use inline styles or CSS. Only basic HTML.",
        ai_compute_depends=['name', 'title', 'parent_id', 'function',
                            'email', 'phone', 'street', 'city',
                            'state_id', 'country_id', 'zip', 'comment'],
        store=True,
    )

    # ------------------------------------------------------------------
    # Selection — constrained output
    # Demonstrates: Selection field, ref/vat fields, category_id (M2M)
    # ------------------------------------------------------------------
    ai_partner_type_suggestion = fields.Selection(
        [
            ('prospect', 'Prospect'),
            ('customer', 'Customer'),
            ('supplier', 'Supplier'),
            ('partner', 'Partner'),
            ('other', 'Other'),
        ],
        string="AI Suggested Type",
        ai_compute="Based on the following contact data for {name}: "
                   "reference={ref}, tax_id={vat}, "
                   "tags={category_id}, "
                   "is_company={is_company}, job_title={function}, "
                   "parent_company={parent_id.name}. "
                   "Classify as exactly ONE of these options: "
                   "prospect, customer, supplier, partner, other. "
                   "Respond ONLY with the keyword, nothing else.",
        ai_compute_depends=['name', 'ref', 'vat', 'category_id',
                            'is_company', 'function', 'parent_id'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Text — One2many (child_ids) and Many2many (category_id) in prompt
    # Demonstrates: relational fields in prompt placeholders
    # ------------------------------------------------------------------
    ai_contacts_summary = fields.Text(
        "AI Contacts Summary",
        ai_compute="Write a brief summary of the contact network of {name}. "
                   "Sub-contacts (children): {child_ids}. "
                   "Tags/categories: {category_id}. "
                   "Is company: {is_company}. "
                   "Describe the structure briefly in 2-3 sentences.",
        ai_compute_depends=['name', 'child_ids', 'category_id', 'is_company'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Text — One2many with dot-notation (bank_ids)
    # Demonstrates: One2many subfield access in prompt
    # ------------------------------------------------------------------
    ai_bank_summary = fields.Text(
        "AI Bank Summary",
        ai_compute="Summarize the banking information for the contact {name}. "
                   "Bank accounts: {bank_ids}. "
                   "Country: {country_id.name}. Tax ID: {vat}. "
                   "Generate a brief summary of their banking situation in 1-2 sentences. "
                   "If there is no banking data, state that.",
        ai_compute_depends=['name', 'bank_ids', 'country_id', 'vat'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Many2one — AI selects from domain-filtered options
    # Demonstrates: Relational compute, domain filtering (South America)
    # ------------------------------------------------------------------
    ai_suggested_country = fields.Many2one(
        'res.country',
        string="AI Suggested Country",
        domain=[('code', 'in', [
            'AR', 'BO', 'BR', 'CL', 'CO', 'EC',
            'GY', 'PY', 'PE', 'SR', 'UY', 'VE',
        ])],
        ai_compute="Based on this contact's address: street={street}, "
                   "city={city}, state={state_id.name}, zip={zip}, "
                   "phone={phone}. "
                   "Determine the most likely country from the available options.",
        ai_compute_depends=['street', 'city', 'state_id', 'zip', 'phone'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Many2many — AI selects multiple tags from all options
    # Demonstrates: Relational compute, no domain (all records)
    # ------------------------------------------------------------------
    ai_suggested_tags = fields.Many2many(
        'res.partner.category',
        relation='partner_ai_suggested_tags_rel',
        column1='partner_id',
        column2='category_id',
        string="AI Suggested Tags",
        ai_compute="Based on this contact's data: name={name}, "
                   "email={email}, phone={phone}, "
                   "is_company={is_company}, job_title={function}, "
                   "company={parent_id.name}, city={city}, "
                   "country={country_id.name}, notes={comment}. "
                   "Select the most relevant tags/categories from the available options.",
        ai_compute_depends=['name', 'email', 'phone', 'is_company',
                            'function', 'parent_id', 'city', 'country_id', 'comment'],
        store=True,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Vision — Image to Text (capable_vision)
    # Demonstrates: Describing/analyzing an image with a vision model
    # ------------------------------------------------------------------
    ai_photo_description = fields.Text(
        "AI Photo Description",
        ai_compute="Describe the person in this photo in detail: physical appearance, "
                   "clothing, expression, background, and overall impression. "
                   "Write 2-3 sentences in a professional tone. "
                   "Photo: {image_1920}",
        ai_compute_depends=['image_1920'],
        store=True,
    )

    # ------------------------------------------------------------------
    # Image Generation — Text to Image (capable_image_generation)
    # Demonstrates: Generating an image from text fields (no image input)
    # ------------------------------------------------------------------
    ai_logo = fields.Image(
        "AI Logo",
        max_width=1024,
        max_height=1024,
        ai_compute="Generate a modern, minimalist corporate logo for the company "
                   "'{name}'. The logo should be clean, professional, and suitable "
                   "for business use. Use a simple color palette. "
                   "The company is located in {city}, {country_id.name} "
                   "and works in the following area: {function}.",
        ai_compute_depends=['name', 'city', 'country_id', 'function'],
        store=True,
    )

    # ------------------------------------------------------------------
    # Image Editing — Image to Image (capable_image_edit)
    # Demonstrates: Transforming an image with a native multimodal model
    # ------------------------------------------------------------------
    ai_image_ghibli = fields.Image(
        "AI Ghibli Portrait",
        max_width=1024,
        max_height=1024,
        ai_compute="Transform this photo into a Studio Ghibli style illustration. "
                   "Maintain the subject's key features, face shape, hairstyle, "
                   "and expression while adapting them into a soft, hand-drawn aesthetic. "
                   "Use warm, vibrant colors, painterly shading, and delicate lighting. "
                   "The background should feel whimsical and rich in detail, "
                   "like a frame from a Ghibli movie. "
                   "Photo reference: {image_1920}",
        ai_compute_depends=['image_1920', 'name'],
        store=True,
    )

    # ------------------------------------------------------------------
    # Document: PDF — AI generates HTML → wkhtmltopdf converts to PDF
    # Demonstrates: ai_document_type='pdf', Binary field, no new deps
    # ------------------------------------------------------------------
    ai_partner_report = fields.Binary(
        "AI Report (PDF)",
        ai_document_type='pdf',
        ai_compute="Generate a professional HTML report for the contact {name}. "
                   "Company: {parent_id.name}. Job title: {function}. "
                   "Email: {email}. Phone: {phone}. "
                   "Address: {street}, {city}, {state_id.name}, "
                   "{country_id.name} {zip}. "
                   "Tags: {category_id}. Notes: {comment}. "
                   "Include a header with the contact name, a summary section, "
                   "contact details in a table, and a brief analysis. "
                   "Use clean HTML with <h1>, <h2>, <table>, <p> tags.",
        ai_compute_depends=['name', 'parent_id', 'function', 'email', 'phone',
                            'street', 'city', 'state_id', 'country_id', 'zip',
                            'category_id', 'comment'],
        store=True,
    )
    ai_partner_report_filename = fields.Char(
        compute='_compute_ai_partner_report_filename',
    )

    # ------------------------------------------------------------------
    # Document: Excel — AI generates JSON → openpyxl converts to xlsx
    # Demonstrates: ai_document_type='xlsx', Binary field, no new deps
    # ------------------------------------------------------------------
    ai_partner_spreadsheet = fields.Binary(
        "AI Spreadsheet (Excel)",
        ai_document_type='xlsx',
        ai_compute="Create a spreadsheet summarizing this contact's profile. "
                   "Contact: {name}. Company: {parent_id.name}. "
                   "Job title: {function}. Email: {email}. Phone: {phone}. "
                   "City: {city}. Country: {country_id.name}. "
                   "Website: {website}. Tax ID: {vat}. "
                   "Is company: {is_company}. Language: {lang}. "
                   "Create meaningful columns and rows with the data. "
                   "Include a 'Field' column and a 'Value' column.",
        ai_compute_depends=['name', 'parent_id', 'function', 'email', 'phone',
                            'city', 'country_id', 'website', 'vat',
                            'is_company', 'lang'],
        store=True,
    )
    ai_partner_spreadsheet_filename = fields.Char(
        compute='_compute_ai_partner_spreadsheet_filename',
    )

    @api.depends('name')
    def _compute_ai_partner_report_filename(self):
        for record in self:
            name = (record.name or 'report').replace(' ', '_')
            record.ai_partner_report_filename = '%s_report.pdf' % name

    @api.depends('name')
    def _compute_ai_partner_spreadsheet_filename(self):
        for record in self:
            name = (record.name or 'spreadsheet').replace(' ', '_')
            record.ai_partner_spreadsheet_filename = '%s_data.xlsx' % name
