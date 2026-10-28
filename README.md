# OS AI — Open Source AI Modules for Odoo

[![License: LGPL-3](https://img.shields.io/badge/licence-LGPL--3-blue.svg)](https://www.gnu.org/licenses/lgpl-3.0)
[![Odoo versions](https://img.shields.io/badge/odoo-15%20%7C%2016%20%7C%2017%20%7C%2018%20%7C%2019%20%7C%2020-blueviolet.svg)](#compatibility)

A collection of open-source modules that bring AI capabilities to Odoo. Powered by [litellm](https://github.com/BerriAI/litellm) for multi-provider support — use cloud APIs, local models via [Ollama](https://ollama.com), or any OpenAI-compatible endpoint.

---

## Available Addons

| Addon | Summary |
|---|---|
| [os_ai](os_ai/) | Core AI infrastructure — provider configuration, logging, and prompt management |
| [os_ai_fields](os_ai_fields/) | AI-computed fields — adds `ai_compute` parameter to any Odoo field |
| [os_ai_fields_demo](os_ai_fields_demo/) | Demo — 18 example AI fields on `res.partner` covering all supported types |

> More modules coming soon (agents, workflows, etc.)

---

## os_ai — Core AI Infrastructure

Base module that provides:

- **Provider management** — Configure multiple AI providers with priority ordering
- **Capability flags** — Text, vision, image generation, image editing — with auto-detect
- **Unified API** — Single `call_llm()` method that routes to the right provider
- **Logging** — AI Logs with prompts, response, tokens, cost and duration (written by OS AI Computed Fields for every call it makes; other modules log with `os.ai.log`)
- **Prompt templates** — Reusable, editable prompt templates managed from the UI
- **Per-provider settings** — temperature (model default or custom), reasoning effort, structured output mode, timeout and output limit
- **Model lifecycle** — retired or deprecated models are flagged with a suggested replacement, applied only on request
- **Access control** — API keys and logs (full prompts and answers) are restricted to administrators

### Provider Settings

| Setting | Default | Notes |
|---|---|---|
| Temperature | Model default | Not sent unless set to *Custom*. Several current models reject it (Claude Opus 4.7+ and Claude 5, GPT-6 with reasoning) and Gemini 3 recommends the default. If a model rejects a custom value, the call is retried once without it |
| Reasoning Effort | empty | `none` to `high`, sent only when set; accepted values depend on the model (levels above `high`, such as `xhigh` or `max`, are not offered). Field values are short: the lowest effort is usually enough and much cheaper (tested: `none` for GPT-6 Luna/Sol and DeepSeek Flash, `low` for GPT-6 Astra/6.1 Sol, Claude 5.x, Gemini 3.x and Grok 4.x). For DeepSeek the level itself is sent too (`reasoning_effort`), not only thinking on/off |
| Structured Output | JSON Schema | *JSON mode* for providers that only guarantee valid JSON (e.g. DeepSeek, which has no JSON Schema output: *Auto-detect* keeps JSON mode), *Instructions only* for models without structured outputs. In both, the expected JSON is described in the prompt. Answers are always validated before being stored |
| Max Output Tokens | 0 (model default) | Truncated answers are never used |
| Timeout | 180 s | Per call (0 = no limit); shortened to the time left of the cron's budget |

### Supported Providers

Calls go through [litellm](https://docs.litellm.ai/docs/providers). These provider types are built in:

| Provider | Type | Example Models |
|---|---|---|
| **OpenAI** | `openai` | gpt-6-luna, gpt-6-sol, gpt-5.4, gpt-image-2, etc. |
| **Google Gemini** | `gemini` | gemini-3.8-flash, gemini-3.5-flash-lite, gemini-3.1-flash-image (Nano Banana 2), etc. |
| **Anthropic** | `anthropic` | claude-sonnet-5-5, claude-opus-5-5, claude-haiku-4-5, etc. |
| **xAI** | `xai` | grok-4.7, grok-4.3, grok-imagine-image, etc. |
| **DeepSeek** | `deepseek` | deepseek-flash, deepseek-v4-pro |
| **Mistral** | `mistral` | mistral-medium-3.5, mistral-large-2512, etc. |
| **Ollama** | `ollama` | Any locally hosted model (Qwen, Gemma, Phi, Llama, etc.) |
| **TypeSafe** (experimental) | `typesafe` | jev-1.14.0, jev-1.13.0, jev-latest — decision models, see [Decision engine](#decision-engine-experimental) |

> Other services that expose an **OpenAI-compatible API** (Kimi, OpenRouter, vLLM, LM Studio, LocalAI...) work with the *OpenAI* type and their *Base URL*; enter the model name that endpoint expects (e.g. `vendor/model` for OpenRouter), without a litellm provider prefix. Services with their own API and authentication need a provider type of their own.

#### Kimi (Moonshot AI)

Kimi K3.1 Pro works as an OpenAI-compatible service. It was tested with real calls: JSON Schema answers, images, and cron batches of several records.

| Setting | Value |
|---|---|
| Provider | *OpenAI* |
| Base URL | `https://api.moonshot.ai/v1` |
| Model Name | `kimi-k3.1-pro`, or `kimi-k3.1-pro-highspeed` (faster) |
| Reasoning Effort | *Low*: Kimi always reasons, and its default is `max`, much slower and more expensive |
| Temperature | *Model default*: Kimi uses a fixed temperature |
| Structured Output | JSON Schema |

The cost shows as *unknown* in AI Logs until litellm lists the price of these models. Pro and Pro Highspeed have different rates; see [Kimi's current API pricing](https://platform.kimi.ai/docs/pricing/chat) for the selected model. *Auto-detect* does not recognize these models; set the capabilities by hand: *Text*, and *Vision* if you send images.

### Running Local Models with Ollama

[Ollama](https://ollama.com) lets you run models locally without API keys or cloud costs:

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull a model: `ollama pull qwen2.5:3b` (or `gemma3:4b`, `phi4-mini`, `phi4`, etc.)
3. In Odoo, create a provider with type **Ollama** and set the model name — no API key needed

Small models like `qwen2.5:3b` or `phi4-mini` (2-4 GB) work well for text fields on modest hardware.

---

## os_ai_fields — AI-Computed Fields

Adds new parameters to any Odoo field, allowing it to be computed by an LLM — analogous to native `compute` but powered by AI:

```python
from odoo import fields, models

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    ai_summary = fields.Text(
        "AI Summary",
        ai_compute="Summarize this sale order for {partner_id.name}: "
                   "lines={order_line}, total={amount_total}, "
                   "date={date_order}.",
        ai_compute_depends=['partner_id.name', 'order_line', 'amount_total', 'date_order'],
        store=True,
    )
```

That's it. The field will be computed automatically by the cron job whenever the dependencies change.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ai_compute` | `str` | — | Prompt template with `{field}` placeholders |
| `ai_compute_depends` | `list` | `[]` | Fields that trigger recomputation. List what the prompt reads, with dotted paths for related values (`partner_id.name`: `partner_id` alone does not react to a renamed partner). A relational placeholder such as `{category_id}` shows display names: depend on them too (`category_id.name`). A warning is logged at startup otherwise. `{__date__}` does not recompute anything by itself |
| `ai_compute_async` | `bool` | `True` | `True`: cron-based (default), `False`: compute on save |
| `ai_compute_batch_size` | `int` | `40` | Max records per LLM call |
| `ai_compute_options_limit` | `int` | `200` | Max Many2one/Many2many candidates offered to the LLM; above it the field is not computed (narrow the `domain`) |
| `ai_document_type` | `str` | `None` | `'pdf'` or `'xlsx'` for Binary fields |

### Prompt Placeholders

| Placeholder | Resolves to |
|---|---|
| `{name}` | Value of field `name` on the record |
| `{partner_id.name}` | Dot-notation for related fields |
| `{order_line}` | One2many/Many2many display names joined with `, ` |
| `{image_1920}` | Binary/Image fields — passed as base64 to vision models |
| `{__date__}` | Current date (YYYY-MM-DD) |
| `{__user__}` | Current user's name |
| `{__company__}` | Name of the record's company (current company for shared records or models without company) |

An empty value gives an empty text; a Boolean gives `True` or `False` and a number `0` stays `0`. A placeholder that names no field, or a value that cannot be read, stops the computation of the record with an error in **AI Computation Errors**; text between braces that is not a field path (e.g. a JSON example in the prompt) is kept as is.

AI fields are computed as superuser (`compute_sudo=True`): the prompt and the relational options may include records the user who triggered the computation cannot read. With `compute_sudo=False` (only together with `ai_compute_async=False`: the cron has no user), the user's access rules apply.

### Supported Field Types

**Text**: Char, Text, Html — with optional `translate=True` for automatic multi-language generation

**Numeric**: Integer, Float — LLM output is parsed and validated

**Date / Datetime**: ISO 8601 (`YYYY-MM-DD`, `2026-10-24T12:00:00+03:00`); offsets are converted to UTC, anything else is rejected

**Boolean** — validated true/false answer

**Selection**: the options are added to the prompt and requested as a JSON Schema `enum`; the answer is mapped to the option key

**Relational**: Many2one, Many2many — LLM receives available options (filtered by the field `domain`, evaluated for each record, and by company when `check_company=True`) and picks by ID from an `enum`. IDs outside the options are never stored

**Images**: Image/Binary fields with three capabilities:
  - *Vision* (image to text) — describe or analyze an image
  - *Generation* (text to image) — create images from a text prompt
  - *Editing* (image to image) — transform an existing image

**Documents**: Binary fields with `ai_document_type`:
  - `'pdf'` — LLM generates HTML, converted to PDF with the database's report engine (wkhtmltopdf, installed on the server; in Odoo 20 the configured PDF engine). The HTML is sanitized first: no scripts, frames, forms, external stylesheets, fonts or images (only embedded `data:` images), so the engine never fetches anything from the server
  - `'xlsx'` — LLM generates structured JSON, converted to Excel with openpyxl. Text cells are never formulas; plain numbers and true/false become numbers and booleans, identifiers such as `00123`, `+5491112345678` or long references stay text

### How It Works

1. Any field with `ai_compute` gets an automatic compute method injected at registry build time
2. By default (async), the compute marks the record as *pending* — the actual LLM call happens in a background cron job
3. The system detects the required capability (text, vision, image generation, etc.) and routes to the best available provider
4. Multiple records are processed in a single LLM call using structured JSON schemas for reliable parsing
5. For `translate=True` fields, all installed languages are generated in one call
6. Every call is logged with full details (prompts, response, tokens, cost, duration), also when its answer is not stored

### Capability Routing

The system automatically determines what each field needs:

| Scenario | Capability used |
|---|---|
| Text field, no image in prompt | `capable_text` |
| Text field, image in prompt | `capable_vision` |
| Image/Binary field, no image in prompt | `capable_image_generation` |
| Image/Binary field, image in prompt | `capable_image_edit` |
| Binary field with `ai_document_type` | `capable_text` |

### Decision Engine (experimental)

Boolean, Selection and Many2one fields can be computed by a **decision model** (e.g. [TypeSafe Jev](https://docs.typesafe.ai)) instead of a text LLM. A decision model does not write text: it answers a yes/no or one-of-N question with probabilities, which fits closed answers well and is fast and cheap (Jev: input tokens only).

It is opt-in per field — a decision provider never changes fields that use the LLM:

```python
ai_is_b2b = fields.Boolean(
    "Business contact",
    ai_compute_engine='decision',
    ai_compute="Is {name} a business rather than an individual person? "
               "Job title: {function}. Website: {website}.",
    ai_compute_depends=['name', 'function', 'website'],
    store=True,
)
```

| Parameter | Default | Description |
|---|---|---|
| `ai_compute_engine` | `'llm'` | `'decision'` to use a provider with *Typed Decisions* |
| `ai_decision_threshold` | `0.5` | Boolean: probability of "yes" above which the value is True |
| `ai_decision_margin` | `0.15` | Boolean: uncertain zone around the threshold — the value is kept |
| `ai_decision_min_confidence` | `0.5` | Selection / Many2one: minimum confidence to store the choice |

How it works:
- The prompt placeholders become the data sent to the model (`state`); the instructions reference them by name and contain no record data
- Selection options (English labels) and Many2one candidates (field `domain`) are the possible answers, plus *none of the options* — at most 254 options
- Uncertain answers keep the current value and are listed as *Low confidence* in **AI Computation Errors**; they are not retried until a dependency changes
- Text only (prompts with images are not supported) and best in English; arithmetic, counting and date comparisons should stay in code
- Calls go through litellm's Decisions API (`litellm.systemone`, System One format), like the LLM calls: logged in **AI Logs** with tokens and the cost from litellm's price list. Rate limits and overloads reported by the provider are retried twice (honouring `Retry-After`, at most 10 s); any other failure is retried by the cron
- Accepted limitation: litellm validates the answer before the module sees it and converts compatible values (the text `"0.9"` to 0.9, `true` to 1.0); the module cannot see the original types and then requires probabilities and confidences in [0, 1] and an option of the question. TypeSafe's API returns numbers

### Reliability

- **Answers are validated before being stored** — a failed call, an invalid, truncated, partial or duplicated answer, or a value outside the field's options never replaces the previous value: the record stays pending
- **Durable failure state** — failed records are listed in **Settings > Technical > OS AI > AI Computation Errors** with the last error. Temporary errors (rate limit, timeout, invalid answer) are retried after 5, 15, 60 and 240 minutes; permanent ones (bad credentials, unknown model, value rejected) wait for the *Retry* button. Changing a dependency of the field computes it again
- **Per-batch commits** — progress is saved even if the cron is interrupted; each value is stored in its own savepoint
- **Concurrent edits** — if a record or one of its dependencies (e.g. a contact added to a company) is modified while the LLM is answering, the stale answer is discarded and the record is computed again
- **Time budget** — each cron run starts no call, retry or image download after `os_ai_fields.cron_time_budget` seconds (default 600), and gives each one at most the time left; the automatic retries of the provider SDK (e.g. the OpenAI client) are disabled there, the cron retries instead; remaining records stay pending. The budget is cooperative: the batch in progress still finishes its database work. Each run reads at most 10,000 pending records per field and starts with a different field, which spreads the work but is no strict fairness guarantee
- **Configuration errors** (unknown placeholder, unknown `ai_compute_engine`, invalid decision parameters, `compute_sudo=False` on an asynchronous field) are logged as errors and shown in **AI Computation Errors** as one line for the field (record ID 0); the records stay pending and the line disappears as soon as the field can be computed again. Fields computed on save (`ai_compute_async=False`) only log them
- Stops a field after 3 consecutive batches without any update — prevents hammering a provider that is down

### Prompt Templates (UI)

Prompts can be overridden from **Settings > Technical > OS AI > AI Prompts** without modifying code. Prompt records are auto-created when modules with `ai_compute` fields are installed. Customized prompts are kept when the module is updated; the prompt defined in the code stays visible and can be restored with *Restore default*.

---

## Examples

### Text with auto-translation

```python
ai_bio = fields.Text(
    translate=True,  # generates in ALL installed languages in one LLM call
    ai_compute="Write a bio for {name} from {country_id.name}. Date: {__date__}",
    ai_compute_depends=['name', 'country_id'],
    store=True,
)
```

### Numeric scoring

```python
ai_score = fields.Integer(
    ai_compute="Rate profile completeness 0-100: name={name}, email={email}, phone={phone}",
    ai_compute_depends=['name', 'email', 'phone'],
    store=True,
)
```

### Selection — AI classification

```python
ai_type = fields.Selection(
    [('prospect', 'Prospect'), ('customer', 'Customer'), ('supplier', 'Supplier')],
    ai_compute="Classify {name} based on: vat={vat}, job={function}, company={parent_id.name}",
    ai_compute_depends=['name', 'vat', 'function', 'parent_id'],
    store=True,
)
```

### Relational — AI picks from filtered options

```python
ai_country = fields.Many2one(
    'res.country',
    domain=[('code', 'in', ['AR', 'BR', 'CL', 'UY'])],
    ai_compute="Guess the country from: city={city}, phone={phone}",
    ai_compute_depends=['city', 'phone'],
    store=True,
)

ai_tags = fields.Many2many(
    'res.partner.category',
    ai_compute="Select relevant tags for {name}, email={email}",
    ai_compute_depends=['name', 'email'],
    store=True,
)
```

### Vision — describe an image

```python
ai_description = fields.Text(
    ai_compute="Describe this photo in detail: {image_1920}",
    ai_compute_depends=['image_1920'],
    store=True,
)
```

### Image generation and editing

```python
ai_logo = fields.Image(
    ai_compute="Generate a minimalist logo for the company '{name}'",
    ai_compute_depends=['name'],
    store=True,
)

ai_ghibli = fields.Image(
    ai_compute="Transform this photo into Studio Ghibli style: {image_1920}",
    ai_compute_depends=['image_1920'],
    store=True,
)
```

### Documents — PDF and Excel

```python
ai_report = fields.Binary(
    "Report (PDF)",
    ai_document_type='pdf',
    ai_compute="Generate an HTML report for {name} with contact details and analysis",
    ai_compute_depends=['name', 'email', 'phone'],
    store=True,
)

ai_sheet = fields.Binary(
    "Data (Excel)",
    ai_document_type='xlsx',
    ai_compute="Create a spreadsheet summarizing {name}'s profile",
    ai_compute_depends=['name', 'email', 'phone'],
    store=True,
)
```

---

## Installation

### Dependencies

- **Odoo** 15.0, 16.0, 17.0, 18.0, 19.0 or 20.0
- **Python** 3.10+ (Odoo 20: 3.12+, PostgreSQL 16+)
- **litellm** 1.104.3 or newer — multi-provider LLM routing library. 1.104.3 is the first release with `litellm.systemone()`, the Decisions API call used by the decision engine (1.104.2 only had `litellm.decisions()`); older releases also drop the reasoning effort of GPT-6 and Claude 5.x models or fail to import on Python 3.10. OS AI Base refuses to install or upgrade with an older one. Tested with 1.104.3, 1.105.0 and 1.106.3. Never use 1.82.7 or 1.82.8 (compromised on PyPI in March 2026):

```bash
pip install "litellm>=1.104.3"
```

litellm downloads its up-to-date model map (capabilities and prices) when Odoo starts. Without internet access, or with `LITELLM_LOCAL_MODEL_COST_MAP=True`, it uses the map bundled in the package: use litellm 1.105.0 or newer then, whose bundled map knows every model listed above (1.104.x does not know Claude Sonnet 5.5 nor Jev 1.14). A model released after your litellm version may get no reasoning effort, no native structured output or an unknown cost. Allow access to `raw.githubusercontent.com` or keep litellm up to date.

For local models, install [Ollama](https://ollama.com) separately.

### Setup

1. Clone the branch of your Odoo version (15.0 to 20.0) into your Odoo addons path:

```bash
git clone -b 17.0 https://github.com/alainbloos/odoo_os_ai.git
```

2. Add the path to `--addons-path` in your Odoo configuration.

3. Install **OS AI Computed Fields** from the Apps menu (it will pull **OS AI Base** automatically).

4. Enable **Developer Mode** (Settings > General Settings > Developer Tools) and configure at least one AI provider in **Settings > Technical > OS AI > AI Providers**.

5. Optionally install **OS AI Fields Demo** to see working examples on the Contact form. Its demo providers are created archived: add your API key to one of them and activate it (an active provider without key would be chosen first and fail).

### Upgrading

Read the [changelog](CHANGELOG.md) first. Upgrading from 1.0 to 1.1 requires litellm 1.104.3 or newer (1.105.0 or newer without internet access, see *Dependencies*), restricts API keys and AI Logs to administrators, keeps the temperature of your existing providers and no longer overwrites prompts customized in the UI. Update the code, then upgrade **OS AI Base** (or run Odoo with `-u os_ai`).

### Configuration

> **Note:** The Technical menu and several of these options are only visible with **Developer Mode** enabled. Activate it from Settings > General Settings > Developer Tools, or by adding `?debug=1` to the URL.

- **AI Providers**: Settings > Technical > OS AI > AI Providers
- **Prompt templates**: Settings > Technical > OS AI > AI Prompts
- **AI Logs**: Settings > Technical > OS AI > AI Logs
- **System prompt**: Settings > OS AI Fields
- **Cron interval**: Settings > Technical > Automation > Scheduled Actions > *AI Fields: Process Pending Computations* (default: every minute). Each run stops after `os_ai_fields.cron_time_budget` seconds (system parameter, default 600); records not reached stay pending for the next run

---

## Compatibility

| Odoo Version | Branch | Status |
|---|---|---|
| 20.0 | `20.0` | Tested |
| 19.0 | `19.0` | Tested |
| 18.0 | `18.0` | Tested |
| 17.0 | `17.0` | Tested |
| 16.0 | `16.0` | Tested |
| 15.0 | `15.0` | Tested |

---

## Logging

Every call made by OS AI Computed Fields is recorded in **Settings > Technical > OS AI > AI Logs**, also when its answer is discarded (invalid, or rolled back because the record changed meanwhile: the provider already billed it):

- Provider and model used
- System prompt and user prompt
- Full response and raw JSON
- Token usage (prompt / completion / total)
- Cost in USD and its source (reported by litellm, estimated, or unknown — 0 does not mean free)
- Duration in seconds
- Success or error with message, and how many records were actually updated

Logs contain full prompts and answers: they are only readable by administrators. Modules that call `call_llm()` or `call_decision()` directly create their own entry, e.g. `env['os.ai.log'].sudo().log_call(vals)` (`log_call()` uses the caller's access rights). Logs are not purged automatically: delete old ones from the list view if they grow too much.

---

## License

[LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html)

## Author

Alain Bloos — [alainbloos@gmail.com](mailto:alainbloos@gmail.com)
