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
| 1 — Tracking + ingestion | [tracking/](tracking/) | `POST /track`, `POST /identify`, `POST /locate` (opt-in precise location), event backfill on identify |
| 2 — Scoring (two-stage)  | [scoring/](scoring/)   | Stage A deterministic features + [engagement rollup](scoring/engagement.py) → [stage rules](scoring/rules.py) → Stage B Gemini classification (strict JSON) |
| 3 — Decision engine      | [decision/](decision/) | Gemini decision (strict JSON) + guardrails validating every decision |
| 4 — Execution (stubbed)  | [execution/](execution/) | Abstract `Sender` + stub email/WhatsApp senders, consent re-check at send time |
| 5 — Dashboard (read-only)| [dashboard/](dashboard/) | JSON routes + minimal HTML: leads, decisions, sent-messages |
| Shared | [config/](config/), [db/](db/), [llm/](llm/) | Config loader + files, DB pool/repositories, direct Gemini client |
| Test harness | [scripts/](scripts/) | `replay.py` feeds seeded journeys (TOFU / MOFU / BOFU) via HTTP |

## How a funnel stage is earned

A page visit on its own is a weak signal — anyone can open `/pricing` for two
seconds. Stages are awarded on **measured behavior**:

* **Active time, not wall clock.** The snippet counts a second only when the tab
  is visible *and* the visitor moved, scrolled or typed within the last minute. A
  pricing tab left open in the background counts for nothing.
* **Qualification.** Each page type carries a `min_seconds` in
  [config/page_types.yaml](config/page_types.yaml). A visit that doesn't clear it
  is a bounce and is excluded from every count that follows.
* **Rules first.** [config/stage_rules.yaml](config/stage_rules.yaml) holds ordered
  rules — first match wins. Conditions can test time on a page type or URL, clicks
  (including button label), time inside a named section, or the journey overall.
* **Gates.** When no rule matches, the model's stage is kept only if it clears that
  stage's minimum evidence; otherwise it's downgraded a step.
* **Explainable.** The rule that fired (or the gate that downgraded the model) is
  stored on the lead as `stage_source` / `stage_reason` and shown on the journey.

Everything above is editable at **`/settings`** — page types with their dwell
thresholds, the rule list with a condition builder, and the gates. Saves go to the
`site_config` table and apply immediately, no redeploy. Set `mode` there to
`rules_first` (default), `rules_only`, or `llm_only`.

## Two-way messaging

Outreach used to be one-way: the agent decided, sent, and logged. Anyone replying
to a "WhatsApp Us" link vanished. [messaging/](messaging/) adds the conversation
around the send — inbound capture, threading, and delivery state.

**The 24-hour window is the rule that shapes everything.** WhatsApp only allows
free-form text within 24 hours of the *contact's* last message; outside it Meta
requires an approved template. That is per-conversation state — it moves every
time they write to you — so it lives on the conversation row and is consulted at
send time. `META_WA_MESSAGE_TYPE` is now a ceiling, not the decision: a text-mode
deployment automatically switches to your template when a contact goes cold
(and stays on text if no template is configured).

Setup — in the Meta app dashboard, WhatsApp → Configuration → Webhook:

| Field | Value |
|---|---|
| Callback URL | `https://YOUR_APP/webhooks/whatsapp` |
| Verify token | whatever you set as `META_WA_VERIFY_TOKEN` |
| Subscribe to | `messages` |

Then set `META_WA_APP_SECRET` (Meta app secret). Every POST is checked against
its `X-Hub-Signature-256` HMAC; without the secret the endpoint returns 503
rather than trusting an unauthenticated public URL.

Meta allows **one callback URL per app**. If another service needs the same
events, point Meta here and set `META_WA_FORWARD_URL` — verified payloads are
relayed on verbatim after we store them.

What it does:
* **Inbound** — replies land in a thread. An unknown number becomes a lead
  (named from their WhatsApp profile) so it gets scored like any other, but with
  **no** consent flags: writing in authorizes a reply, not cold outreach later.
* **Idempotent** — Meta redelivers until it gets a 2xx; every message and receipt
  is deduplicated on its `wamid`.
* **Delivery state** — sent → delivered → read → failed, applied forward-only so
  out-of-order receipts can't undo a later one. A receipt that arrives before we
  finish recording the send is parked and applied when the id lands.
* **Identity** — phone matching compares digits, so `+91 96995 30806` from a form
  and `919699530806` from WhatsApp are one lead, not two.

API: `GET /api/conversations`, `GET /api/conversations/{id}`,
`POST /api/conversations/{id}/reply`, `POST /api/conversations/{id}/read`.
A human reply skips the outreach guardrails (rate limit, send window) — those
govern the agent's cold outreach, not a person answering someone who wrote in.

## The live map

[Leaflet](https://leafletjs.com) (BSD-2) over [OpenStreetMap](https://www.openstreetmap.org)
tiles (ODbL) — both free and open source, no API key, no account. Leaflet is
vendored under [static/vendor/](static/vendor/) and served from our own origin, so
the dashboard has no CDN dependency.

Zoom runs from 2 (whole world) to 19 (individual buildings), which is what makes a
consented GPS fix worth having: you can see the actual street. Scroll to zoom,
drag to pan, **Fit all** reframes every visitor. The view is only auto-fitted
once — the 3-second refresh never yanks your pan or zoom back.

GPS visitors are drawn with their accuracy radius as a circle, so a ±2 km Wi-Fi
guess visibly differs from a ±12 m lock.

Two things worth knowing:
* OSM tiles are fetched from `tile.openstreetmap.org` at render time. Their
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) covers
  low-volume use like an internal dashboard; attribution is displayed as required.
  Tile requests reveal the viewport to the OSM servers — not visitor data, but
  worth knowing before pointing this at a customer-facing page.
* If tiles can't be reached (offline, blocked network), the map falls back to the
  self-hosted `world.js` outline and says so, rather than showing an empty grey box.

## How precise is a visitor's location?

Two sources, and the difference matters:

| Source | Consent | Typical precision | What you get |
|---|---|---|---|
| **IP lookup** (automatic) | none needed | City — **often the wrong one** | country, region, city, postal, ISP |
| **Browser Geolocation** (opt-in) | visitor must accept the prompt | 10–100 m | street, neighbourhood, city, postal, exact radius |

An IP resolves to the **ISP's gateway, not the person**. On fixed broadband it's
usually the right city; on mobile it frequently isn't — a Jio subscriber in Pune
commonly resolves to Mumbai. No IP database fixes this, so IP results are stored
with `location_source='ip'` and shown as "approx · from IP (<ISP>)". The ISP name
is your tell: a mobile carrier means "somewhere in this state".

To learn *where in Pune*, the visitor has to share it. Put the ask on a button
where it makes sense — never on page load, since an unexplained prompt gets denied
and browsers remember the denial:

```html
<button data-fa-locate>Find my nearest clinic</button>
```

or `funnel.locate().then(r => …)`. On accept, the coordinates are reverse-geocoded
via OpenStreetMap into a street and neighbourhood ("Gopal Krushna Gokhale Path,
Deccan Gymkhana, Pune 411004") and stored with `location_source='gps'` plus the
browser's own accuracy radius. A GPS fix always wins over an IP estimate and is
never overwritten by one. The dashboard grades every fix — `exact · ±12 m`,
`coarse · ±2.4 km`, `approx · from IP` — so a guess never reads as a fact.

Expect most visitors to decline. IP-level stays the norm; the opt-in is what gives
you real addresses for the ones who want something from you in return.

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
   It auto-tracks page views (incl. SPA route changes), clicks on any
   `data-fa-event` element, and **engagement** — active seconds per page, scroll
   depth and time per section — and exposes `funnel.track(type, {metadata})` and
   `funnel.identify({email, phone, email_opt_in, whatsapp_opt_in, consent_source})`.
   Mark the blocks you want timed: `<section data-fa-section="pricing-table">`
   (any `section[id]` is picked up automatically).
3. **Configure** (env): `TRACK_WRITE_KEY` (require the snippet's key on `/track`),
   `CORS_ALLOW_ORIGINS` (your site origin), `EXECUTION_MODE=shadow` (never actually send).
4. **Point config at your site**: `config/page_types.yaml` → your URL patterns and
   per-page qualifying dwell; `config/stage_rules.yaml` → what earns each funnel
   stage; `config/templates.yaml` → your copy; guardrail timezone. All of these are
   editable live from **Settings** (`/settings`) — saved to the DB, no redeploy.
5. **Schedule** scoring + decisions (cron/worker hitting `/score/run` then `/decide/run`).
   With `EXECUTION_MODE=shadow`, `/execute/run` logs would-be sends to `sent_messages`
   without transmitting — watch them on the dashboard.
6. **Go live** later: implement real senders (Postmark/Twilio) in `execution/stubs.py`
   `_LIVE`, set up domain auth + consent capture + unsubscribe, then `EXECUTION_MODE=live`.

Try it locally: **`open http://localhost:8000/demo`** — a sample page wired with the
snippet. Click around and submit the form, then refresh the dashboard to watch the
new visitor flow through scoring → decision.

### "The site won't load" — check DNS before Railway

```bash
./scripts/diagnose.sh
```

Tells you which of the two it is. A resolver that refuses the hostname and a
server that is genuinely down look identical in a browser, and they have nothing
to do with each other: this has already happened once, where a laptop's
router-supplied resolver returned `REFUSED` for the whole `up.railway.app` zone
while the service kept serving every visitor normally.

If it says DNS, pin a resolver so it stops depending on whichever network you
joined:

```bash
sudo networksetup -setdnsservers Wi-Fi 1.1.1.1 1.0.0.1 2606:4700:4700::1111 2606:4700:4700::1001
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

### Custom domain behind Cloudflare (recommended)

Some ISPs refuse to resolve `*.up.railway.app` — observed live on Jio, where the
resolver returns `REFUSED` for that zone while answering everything else. Any
visitor on such a network silently fails to load `track.js`, so you lose their
data without an error anywhere.

Serving the app from your own domain fixes it, but **only if Cloudflare proxies
it**. A DNS-only (grey cloud) record is a CNAME to `*.up.railway.app`, and a
resolver that refuses that zone still fails when it follows the chain. Proxied
(orange cloud), Cloudflare answers with its own IPs and resolves Railway from its
network — the visitor's resolver never sees `railway.app` at all.

1. **Railway** → service → Settings → Networking → *Custom Domain* → enter
   `agent.yourdomain.com`. Railway shows a CNAME target.
2. **Cloudflare** → DNS → add `CNAME  agent → <target>`, proxy **OFF** at first.
   Railway validates and issues its certificate (it checks from its own side, so
   your ISP's DNS is irrelevant here).
3. Once Railway shows the domain as active, set proxy **ON** (orange cloud).
4. **SSL/TLS → Overview → Full (strict)**. "Flexible" causes a redirect loop.
5. Point the snippet at the new host and bump the version so browsers refetch:
   `https://agent.yourdomain.com/track.js?v=2`

Behind Cloudflare the app reads `CF-Connecting-IP` for visitor geo. Without it,
every visitor's location would resolve to the first hop in `X-Forwarded-For`.

Two Cloudflare settings worth checking: **Bot Fight Mode** can block Meta's
webhook POSTs to `/webhooks/whatsapp`, and caching should be left on "Respect
Existing Headers" so `track.js` honours its 5-minute max-age.

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

## Deploy (AWS App Runner + RDS)

`apprunner.yaml` lets App Runner build straight from GitHub — no Docker or ECR.
The app applies `schema.sql` itself on boot, so there's no manual DB step.

1. **RDS**: create a PostgreSQL instance (`db.t4g.micro` is plenty). For a quick
   start make it publicly accessible and allow inbound `5432` (lock the security
   group to your IP / use an App Runner VPC connector for production).
2. **App Runner** → Create service → **Source: GitHub** → your repo → it reads
   `apprunner.yaml`.
3. **Environment variables** (App Runner → Configuration):
   - `DATABASE_URL=postgresql://USER:PASSWORD@ENDPOINT:5432/DBNAME`
   - `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-2.5-flash`
   - `TRACK_WRITE_KEY`, `CORS_ALLOW_ORIGINS=https://engageoagency.com,https://www.engageoagency.com`
   - `EXECUTION_MODE=shadow`, `SITE_ID=default`
4. App Runner returns an HTTPS URL (`https://xxxx.<region>.awsapprunner.com`) and
   uses `/health` as its check. Dashboard at `/dashboard`.
5. Point the GTM tag at that URL.

Prefer one box? A single EC2 instance running `docker compose up` (this repo's
compose file) also works — add Caddy/nginx for HTTPS on a subdomain.

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
