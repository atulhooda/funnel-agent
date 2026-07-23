# Behavioral Funnel Agent — Prototype

A prototype **decision brain** for a behavioral marketing funnel. Its goal is to
**prove the decision logic works against replayed visitor journeys** — it does
**not** send real messages or serve a live site. All external sends are stubbed
(logged only). All model calls are **direct Gemini API** (no LangChain).

> Status: **All 5 layers implemented** — tracking + ingestion + replay; two-stage
> scoring; decision engine + guardrails; stubbed execution; read-only dashboard
> at `/dashboard`.

## Design principles

- **Multi-tenant-ready:** every data table has a `site_id` (constant `'default'`
  for now), anchored by a `sites` table. Nothing hardcodes single-tenancy.
- **Business rules live in config, not code:** funnel logic, scoring thresholds,
  page-type mappings, message templates and guardrail params are read from
  files under [config/](config/) (or optional `site_config` DB rows) — never
  baked into prompts or `if`-statements.
- **Senders behind an interface, stubbed:** `Sender.send(to, message, metadata)`;
  stubs log to `sent_messages` and console. Real providers drop in later.

## Layers

| Layer | Folder | Responsibility |
|------|--------|----------------|
| 1 — Tracking + ingestion | [tracking/](tracking/) | `POST /track`, `POST /identify`, event backfill on identify |
| 2 — Scoring (two-stage)  | [scoring/](scoring/)   | Stage A deterministic features → Stage B Gemini classification (strict JSON) |
| 3 — Decision engine      | [decision/](decision/) | Gemini decision (strict JSON) + guardrails validating every decision |
| 4 — Execution (stubbed)  | [execution/](execution/) | Abstract `Sender` + stub email/WhatsApp senders, consent re-check at send time |
| 5 — Dashboard (read-only)| [dashboard/](dashboard/) | JSON routes + minimal HTML: leads, decisions, sent-messages |
| Shared | [config/](config/), [db/](db/), [llm/](llm/) | Config loader + files, DB pool/repositories, direct Gemini client |
| Test harness | [scripts/](scripts/) | `replay.py` feeds seeded journeys (TOFU / MOFU / BOFU) via HTTP |

## Data model

See [schema.sql](schema.sql): `sites`, `leads`, `identities`, `events`,
`decisions`, `sent_messages`, `site_config`. Every table carries `site_id`.

## Setup (works once the layers are implemented)

```bash
# 1. Python env + deps
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Config
cp .env.example .env        # set DATABASE_URL and GEMINI_API_KEY

# 3. Database
createdb funnel_agent
psql "$DATABASE_URL" -f schema.sql

# 4. Run the API
uvicorn main:app --reload

# 5. Replay seeded visitor journeys into the API
python scripts/replay.py --base-url http://localhost:8000

# 6. Score every visitor — two-stage (needs GEMINI_API_KEY set in .env)
python scripts/score.py --base-url http://localhost:8000

# 7. Decide the next action per lead (engine + guardrails; every decision logged)
python scripts/decide.py --base-url http://localhost:8000

# 8. Execute accepted outreach — STUBBED (logs to sent_messages, transmits nothing)
python scripts/execute.py --base-url http://localhost:8000

# 9. View the dashboard
open http://localhost:8000/dashboard
```

## Going live on your own site (shadow mode)

The prototype proves the brain on replayed journeys. To run it on **real traffic**
without sending anything yet:

1. **Deploy** the app + a managed Postgres; load `schema.sql`. The LLM key stays
   server-side (the browser only posts events).
2. **Add the snippet** to your site, before `</body>`:
   ```html
   <script src="https://YOUR_APP/track.js"
           data-api="https://YOUR_APP" data-key="YOUR_WRITE_KEY" data-site="default"></script>
   ```
   It auto-tracks page views (incl. SPA route changes) and clicks on any
   `data-fa-event` element, and exposes `funnel.track(type, {metadata})` and
   `funnel.identify({email, phone, email_opt_in, whatsapp_opt_in, consent_source})`.
3. **Configure** (env): `TRACK_WRITE_KEY` (require the snippet's key on `/track`),
   `CORS_ALLOW_ORIGINS` (your site origin), `EXECUTION_MODE=shadow` (never actually send).
4. **Point config at your site**: `config/page_types.yaml` → your URL patterns;
   `config/templates.yaml` → your copy; guardrail timezone.
5. **Schedule** scoring + decisions (cron/worker hitting `/score/run` then `/decide/run`).
   With `EXECUTION_MODE=shadow`, `/execute/run` logs would-be sends to `sent_messages`
   without transmitting — watch them on the dashboard.
6. **Go live** later: implement real senders (Postmark/Twilio) in `execution/stubs.py`
   `_LIVE`, set up domain auth + consent capture + unsubscribe, then `EXECUTION_MODE=live`.

Try it locally: **`open http://localhost:8000/demo`** — a sample page wired with the
snippet. Click around and submit the form, then refresh the dashboard to watch the
new visitor flow through scoring → decision.

### Sending real WhatsApp (Meta Cloud API)

A real sender ([execution/meta_whatsapp.py](execution/meta_whatsapp.py)) is wired
behind the `Sender` interface. It activates only when `EXECUTION_MODE=live` **and**
the token + phone-number-id are set — otherwise the stub is used, so it's safe by
default. You provide (from Meta Business / your WhatsApp Business Account):

| Env | What it is |
|---|---|
| `META_WA_ACCESS_TOKEN` | System User **permanent** token with `whatsapp_business_messaging` (temp tokens expire ~24h) |
| `META_WA_PHONE_NUMBER_ID` | the WABA phone number's **ID** (not the phone number) |
| `META_WA_MESSAGE_TYPE` | `text` (only inside the 24h window) or `template` (business-initiated outreach) |
| `META_WA_TEMPLATE_NAME` / `_LANG` | an **approved** template for cold outreach |

**Smoke test** (no template of your own needed): every WABA ships the approved
`hello_world` template. Set `EXECUTION_MODE=live`, `META_WA_MESSAGE_TYPE=template`,
`META_WA_TEMPLATE_NAME=hello_world`, `META_WA_TEMPLATE_BODY_PARAM=false`, add your
own number to the app's test allow-list in Meta, then run the pipeline — the
guardrails still require the lead's `whatsapp_opt_in`, and execution re-checks it.

For production outreach you'll create your own **approved marketing/utility
template** and set `META_WA_MESSAGE_TYPE=template` with its name (the decision
engine's message fills the template's body variable). Email stays stubbed until
you add an email provider to `_live_registry()` the same way.

## Deploy (Railway)

The repo ships a `Dockerfile`, `entrypoint.sh` (waits for the DB, applies
`schema.sql` idempotently, binds the host's `$PORT`), and `railway.toml`.

1. **railway.app → New Project → Deploy from GitHub repo** → pick your repo.
   Railway detects the Dockerfile and builds it.
2. **Add Postgres**: in the project, **New → Database → PostgreSQL**.
3. **Set variables** on the app service:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (reference the Postgres service)
   - `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-flash`
   - `TRACK_WRITE_KEY` (any random string), `CORS_ALLOW_ORIGINS=https://engageoagency.com,https://www.engageoagency.com`
   - `EXECUTION_MODE=shadow`, `SITE_ID=default`
4. **Generate a domain**: app service → Settings → Networking → Generate Domain.
   The schema applies on first boot; the dashboard is at `/dashboard`.
5. Point the GTM tag (`FUNNEL_API` + `src`) at that domain.

Locally the same image runs with `docker compose up --build` (app + Postgres).

## Folder structure

```
funnel-agent/
├── schema.sql              # DB schema (this step)
├── main.py                 # FastAPI entry point
├── requirements.txt
├── .env.example
├── config/                 # business rules as config (page types, scoring, guardrails, templates, prompts)
│   └── prompts/
├── db/                     # connection pool + repositories (only place with SQL)
├── llm/                    # direct Gemini client (no LangChain)
├── tracking/               # Layer 1
├── scoring/                # Layer 2
├── decision/               # Layer 3
├── execution/              # Layer 4
├── dashboard/              # Layer 5
│   └── templates/
└── scripts/                # replay.py + journeys.json
```
