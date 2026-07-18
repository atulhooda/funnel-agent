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
