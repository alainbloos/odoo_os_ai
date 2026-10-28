# Changelog

## 1.1.0 — 2026-10-28

Module versions: OS AI Base and OS AI Computed Fields `<odoo>.1.1.0`, OS AI Fields Demo `<odoo>.1.0.1`, for Odoo 15.0 to 19.0 and the new 20.0 branch.

### Before upgrading

- **litellm 1.104.3 or newer is required** — `pip install "litellm>=1.104.3"` (1.105.0 or newer without internet access, see the README). Odoo refuses to install or upgrade OS AI Base with an older one. Never use litellm 1.82.7 or 1.82.8 (compromised on PyPI in March 2026). Tested with 1.104.3, 1.105.0 and 1.106.3.
- **API keys and AI Logs are restricted to administrators.** Logs contain full prompts and answers; internal users can still trigger AI computations.
- **Your providers keep sending their temperature.** The upgrade sets *Temperature* to *Custom* on existing providers; new providers use the model's default. Choose *Model default* for models that reject a temperature (Claude Opus 4.7+, Claude 5, GPT-6 with reasoning).
- **Prompts customized in the UI are no longer overwritten** by module updates (*Restore default* brings back the text from the code).
- **Demo providers:**
  - New installations create them archived: activate the one you add a key to.
  - Retired models are no longer created on new installations.
  - Existing demo providers are not modified; their form shows a warning with the suggested replacement.
- **For developers:**
  - `os.ai.log.log_call()` now uses the caller's access rights; log on behalf of other users with `sudo().log_call(vals)`.
  - Asynchronous AI fields need `compute_sudo=True`, the default.
  - A placeholder that names no field is now an error instead of an empty text.

### Reliability of AI-computed fields

- Records created or edited are now really queued: the pending flag was only cached and the cron never saw them.
- A change of a dependency during the call (e.g. a contact added to a company) discards the answer and computes the record again, even when it was already pending.
- A failed call, or an invalid, truncated, partial or duplicated answer, never replaces the previous value: the record stays pending.
- New menu **Settings > Technical > OS AI > AI Computation Errors** with the last error per record and field. Temporary errors are retried after 5, 15, 60 and 240 minutes (5 attempts). Permanent ones wait for the *Retry* button or a change in a dependency.
- Every answer is validated and converted before being stored:
  - typed values, Selection keys and the Integer range;
  - relational IDs, which must be among the authorized candidates;
  - numbers such as `.5`;
  - dates and times in ISO 8601, with offsets converted to UTC.
- Truncated JSON (also inside a Markdown fence), repeated JSON keys and repeated languages are rejected.
- Relational candidates fail closed: a domain error, no candidates or too many (`ai_compute_options_limit`, default 200) never lets an arbitrary record through. Fields with `check_company` get the candidates of each record's company, following Odoo's own rule of each version (e.g. users of several companies, accounts shared by companies in Odoo 18+). With `compute_sudo=False` the user's access rules apply.
- Each value is stored in its own savepoint. If a record is edited while the model is answering, the stale answer is discarded and the record is computed again.
- Each cron run starts no call, retry or image download after `os_ai_fields.cron_time_budget` seconds (default 600), gives each one at most the time left (without the provider SDK's own retries), reads at most 10,000 pending records per field and starts with a different field each time.
- Configuration errors (unknown placeholder or `ai_compute_engine`, invalid decision parameters) are logged as errors and shown in **AI Computation Errors** as one line for the field, removed when it can be computed again. Placeholders that cannot be read stop the record with an error instead of sending an empty value. Booleans appear as `True`/`False` in prompts, and zeros are kept.
- `{__company__}` is the record's company when it has one (otherwise the user's current company).
- AI Logs show how many records each call updated and where the cost comes from (reported, estimated or unknown — 0 does not mean free). Calls whose answer is discarded, rolled back or invalid are logged too, with their tokens and cost.

### Providers and models

- **Support for the current models**, for example:
  - OpenAI: GPT-6 Luna, Sol, 6.1 Sol and Astra.
  - Anthropic: Claude Sonnet 5.5, Opus 5.5, Fable 5.1 and Haiku 4.5.
  - Google: Gemini 3.8 Flash, 3.5 Flash-Lite and 3.1 Pro.
  - xAI: Grok 4.7 and 4.3.
  - DeepSeek: Flash and V4 Pro.
  - Mistral: Medium 3.5 and Large 3.
  - Images: gpt-image-2, Gemini 3.1 Flash Image and Grok Imagine.
  - Kimi K3.1 Pro (Moonshot AI), through its OpenAI-compatible API. The settings are in the README; the cost shows as unknown until litellm prices it.
- **New provider settings:** *Temperature* (model default or custom), *Reasoning effort*, *Max output tokens*, *Timeout* and *Structured output* (JSON Schema, JSON mode or instructions only). *Auto-detect* fills structured output and decision support from litellm.
- **Closed answers:** Selection, Many2one and Many2many answers are requested as JSON Schema enums; Selection options are listed in the prompt.
- **Retired models:** they are flagged with a suggested replacement, applied only on request.
- **Errors:**
  - Provider errors are classified as temporary or permanent.
  - Truncated or content-filtered answers are refused.
  - Images are sent with their real media type (PNG, JPEG, GIF, WebP); formats vision models cannot read are a permanent error.
- **Documents:**
  - AI PDF documents use the database's report engine.
  - The generated HTML is sanitized: no scripts, frames or external resources, so the engine never fetches anything from the server.
  - A PDF answer that is not HTML is rejected.
  - In Excel, text cells are never formulas and identifiers such as `00123` stay text.
- **DeepSeek:** the reasoning level is sent (litellm only switched thinking on or off), and *Auto-detect* keeps JSON mode: DeepSeek has no JSON Schema output.
- **Instructions only:** the expected JSON is described in the prompt; a Many2many answer may be a plain list of IDs.
- **Provider settings:** timeout, output limit and temperature are validated. litellm's `drop_params` is passed per call instead of changing the process-wide setting.

### Decision engine (experimental, new)

- Boolean, Selection and Many2one fields can opt in with `ai_compute_engine='decision'`. The engine uses a decision model instead of a text LLM: TypeSafe Jev `jev-1.14.0` or `jev-1.13.0`.
- The model answers typed questions with probabilities:
  - Boolean: `ai_decision_threshold` and `ai_decision_margin` define when the answer is True or False.
  - Selection and Many2one: `ai_decision_min_confidence` sets the confidence needed to store the choice, among at most 254 options plus "none of the options".
  - Uncertain answers keep the current value and are listed as *Low confidence*.
- Calls go through litellm's Decisions API (`litellm.systemone`), logged with tokens and cost like any other provider.
- Accepted limitation: litellm converts compatible values of the answer (`"0.9"` to 0.9, `true` to 1.0) before the module checks them.
- Each placeholder gets its own key in the request, and a Selection key `__none__` is refused (reserved for "none of the options").
- Demo: three decision fields in the *Decisions* tab of the contact form, and a *TypeSafe Jev 1.14* provider.

### Odoo 20

- New `20.0` branch, ported from 19.0:
  - access rights use `ir.access`;
  - configuration parameters use `get_str`;
  - pluggable PDF engines;
  - caches are cleared with `transaction.clear()`.
- Requires Python 3.12+ and PostgreSQL 16+. Tested with Odoo Community.

## 1.0.0 — 2026-03-08

- First release: OS AI Base, OS AI Computed Fields and OS AI Fields Demo for Odoo 15.0 to 19.0.
