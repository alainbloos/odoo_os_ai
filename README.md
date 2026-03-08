# OS AI — Open Source AI Modules for Odoo

[![License: LGPL-3](https://img.shields.io/badge/licence-LGPL--3-blue.svg)](https://www.gnu.org/licenses/lgpl-3.0)
[![Odoo versions](https://img.shields.io/badge/odoo-15%20%7C%2016%20%7C%2017%20%7C%2018%20%7C%2019-blueviolet.svg)](#compatibility)

A collection of open-source modules that bring AI capabilities to Odoo. Powered by [litellm](https://github.com/BerriAI/litellm) for multi-provider support — use cloud APIs, local models via [Ollama](https://ollama.com), or any OpenAI-compatible endpoint.

---

## Available Addons

| Addon | Summary |
|---|---|
| [os_ai](os_ai/) | Core AI infrastructure — provider configuration, logging, and prompt management |
| [os_ai_fields](os_ai_fields/) | AI-computed fields — adds `ai_compute` parameter to any Odoo field |
| [os_ai_fields_demo](os_ai_fields_demo/) | Demo — 15 example AI fields on `res.partner` covering all supported types |

> More modules coming soon (agents, workflows, etc.)

---

## os_ai — Core AI Infrastructure

Base module that provides:

- **Provider management** — Configure multiple AI providers with priority ordering
- **Capability flags** — Text, vision, image generation, image editing — with auto-detect
- **Unified API** — Single `call_llm()` method that routes to the right provider
- **Logging** — Every LLM call logged with prompts, response, tokens, cost, and duration
- **Prompt templates** — Reusable, editable prompt templates managed from the UI

### Supported Providers

All routing is handled by [litellm](https://docs.litellm.ai/docs/providers), which supports **100+ LLM providers** and thousands of models. The following are pre-configured as provider types with first-class support:

| Provider | Type | Example Models |
|---|---|---|
| **OpenAI** | `openai` | Any GPT, o-series, DALL-E, gpt-image, etc. |
| **Google Gemini** | `gemini` | Any Gemini model, Imagen, etc. |
| **Anthropic** | `anthropic` | Any Claude model |
| **xAI** | `xai` | Grok models |
| **DeepSeek** | `deepseek` | DeepSeek Chat, Reasoner, etc. |
| **Mistral** | `mistral` | Mistral Large, Pixtral, etc. |
| **Ollama** | `ollama` | Any locally hosted model (Qwen, Gemma, Phi, Llama, etc.) |

> Since litellm handles routing, you can use virtually **any model from any provider** — including self-hosted endpoints via `base_url`. The list above is not exhaustive; it represents the providers with built-in type selection in the UI.

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
        ai_compute_depends=['partner_id', 'order_line', 'amount_total', 'date_order'],
        store=True,
    )
```

That's it. The field will be computed automatically by the cron job whenever the dependencies change.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ai_compute` | `str` | — | Prompt template with `{field}` placeholders |
| `ai_compute_depends` | `list` | `[]` | Fields that trigger recomputation |
| `ai_compute_async` | `bool` | `True` | `True`: cron-based (default), `False`: compute on save |
| `ai_compute_batch_size` | `int` | `40` | Max records per LLM call |
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
| `{__company__}` | Current company's name |

### Supported Field Types

**Text**: Char, Text, Html — with optional `translate=True` for automatic multi-language generation

**Numeric**: Integer, Float — LLM output is parsed and validated

**Selection**: LLM picks from the field's defined options

**Relational**: Many2one, Many2many — LLM receives available options (filtered by `domain`) and picks by ID

**Images**: Image/Binary fields with three capabilities:
  - *Vision* (image to text) — describe or analyze an image
  - *Generation* (text to image) — create images from a text prompt
  - *Editing* (image to image) — transform an existing image

**Documents**: Binary fields with `ai_document_type`:
  - `'pdf'` — LLM generates HTML, converted to PDF via wkhtmltopdf (bundled with Odoo)
  - `'xlsx'` — LLM generates structured JSON, converted to Excel via openpyxl (bundled with Odoo)

### How It Works

1. Any field with `ai_compute` gets an automatic compute method injected at registry build time
2. By default (async), the compute marks the record as *pending* — the actual LLM call happens in a background cron job
3. The system detects the required capability (text, vision, image generation, etc.) and routes to the best available provider
4. Multiple records are processed in a single LLM call using structured JSON schemas for reliable parsing
5. For `translate=True` fields, all installed languages are generated in one call
6. Every call is logged with full details (prompts, response, tokens, cost, duration)

### Capability Routing

The system automatically determines what each field needs:

| Scenario | Capability used |
|---|---|
| Text field, no image in prompt | `capable_text` |
| Text field, image in prompt | `capable_vision` |
| Image/Binary field, no image in prompt | `capable_image_generation` |
| Image/Binary field, image in prompt | `capable_image_edit` |
| Binary field with `ai_document_type` | `capable_text` |

### Cron Resilience

- Per-batch commits — progress is saved even if the cron is interrupted
- Automatic rollback on errors — failed batches don't affect other records
- Stops after 3 consecutive failures for the same field — prevents endless retries if a provider is down

### Prompt Templates (UI)

Prompts can be overridden from **Settings > Technical > OS AI > AI Prompts** without modifying code. Prompt records are auto-created when modules with `ai_compute` fields are installed.

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

- **Odoo** 15.0, 16.0, 17.0, 18.0 or 19.0
- **Python** 3.10+
- **litellm** — multi-provider LLM routing library

```bash
pip install litellm
```

For local models, install [Ollama](https://ollama.com) separately.

### Setup

1. Clone the repository into your Odoo addons path:

```bash
git clone -b 17.0 https://github.com/alainbloos/odoo_os_ai.git
```

2. Add the path to `--addons-path` in your Odoo configuration.

3. Install **OS AI Computed Fields** from the Apps menu (it will pull **OS AI Base** automatically).

4. Enable **Developer Mode** (Settings > General Settings > Developer Tools) and configure at least one AI provider in **Settings > Technical > OS AI > AI Providers**.

5. Optionally install **OS AI Fields Demo** to see working examples on the Contact form.

### Configuration

> **Note:** The Technical menu and several of these options are only visible with **Developer Mode** enabled. Activate it from Settings > General Settings > Developer Tools, or by adding `?debug=1` to the URL.

- **AI Providers**: Settings > Technical > OS AI > AI Providers
- **Prompt templates**: Settings > Technical > OS AI > AI Prompts
- **AI Logs**: Settings > Technical > OS AI > AI Logs
- **System prompt**: Settings > OS AI Fields
- **Cron interval**: Settings > Technical > Automation > Scheduled Actions > *OS AI Fields: Compute pending fields* (default: every 5 minutes)

---

## Compatibility

| Odoo Version | Branch | Status |
|---|---|---|
| 19.0 | `19.0` | Tested |
| 18.0 | `18.0` | Tested |
| 17.0 | `17.0` | Tested |
| 16.0 | `16.0` | Tested |
| 15.0 | `15.0` | Tested |

---

## Logging

Every LLM call is recorded in **Settings > Technical > OS AI > AI Logs**:

- Provider and model used
- System prompt and user prompt
- Full response and raw JSON
- Token usage (prompt / completion / total)
- Estimated cost in USD
- Duration in seconds
- Success or error with message

---

## License

[LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html)

## Author

Alain Bloos — [alainbloos@gmail.com](mailto:alainbloos@gmail.com)
