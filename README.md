# LeadGate

LeadGate is an AI lead-intake engine that talks to customers over Telegram, searches a
real catalog, and hands qualified conversations off to a human by creating a CRM lead
in Odoo, all without a single line of code that knows what it's selling.

A customer messages the bot. The engine's agent loop decides, turn by turn, whether to
search the catalog or create a lead, calls the right tool against Odoo, and replies in
natural language grounded in whatever the tool actually returned. Every catalog item
status change in Odoo is pushed through n8n to a Supabase table (for a live dashboard)
and a MongoDB collection (for an audit log); lead creation itself is a separate,
one-way path (the agent calls `create_lead`, which writes a `crm.lead` record directly
via XML-RPC) and is not part of that sync pipeline. That's the whole system.

## The generalization proof

The interesting part isn't the bot. It's that `engine/core/` (the agent loop, the LLM
client, the adapter interface) contains zero domain-specific code. No "car," no
"bedroom," no vertical-specific vocabulary anywhere in that directory. Everything that
differs between selling cars and selling houses lives in a roughly 40-line adapter
(`engine/adapters/cars.py`, `engine/adapters/real_estate.py`) that declares its own tool
schema and translates tool calls into an Odoo domain filter.

To prove that isn't just a design claim, the same engine was pointed at two unrelated,
real datasets, 350 real used-car listings and 352 real property listings, both seeded
into the same Odoo instance, and evaluated independently on each with a hand-labeled
test suite. See [`eval/REPORT.md`](eval/REPORT.md) for the full methodology, the
synthetic-data disclosure, and the honest story of the bugs found and fixed along the
way. The headline numbers, re-verified live on 2026-09-18 against 93 real end-to-end
test cases:

| Domain | Cases | Tool selection | Slot extraction | Grounding | Task completion |
|---|---|---|---|---|---|
| Cars | 45 | 100.00% | 98.68% | 100.00% | 97.78% (44/45) |
| Real estate | 48 | 97.92% | 98.33% | 100.00% | 95.83% (46/48) |

100% grounding means zero hallucinated facts across both runs: every price, count, and
feature the agent stated was traceable back to a real tool result. None of this is
rounded up. `eval/REPORT.md` also documents the remaining known gaps -- no
price-minimum filter, one residual non-deterministic case, and `RealEstateAdapter`
declaring `property_type`/`bedrooms` in its tool schema but never actually filtering
on them server-side (11 of real_estate's 48 eval cases, 23%, are labeled and graded
around exactly this; fixing it needs new structured columns on the catalog model, not
just an adapter change) -- and the round of fixes an independent adversarial review
confirmed were genuine rather than metric-gaming.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full data-flow diagram and the
reasoning behind running two separate databases downstream of Odoo.

## Setup

This setup was actually run, in this order, to build the working system. Every command
below is taken from the real scripts in this repo, not idealized.

### Prerequisites

- Docker and Docker Compose
- Python 3.11+ and `pip`
- Node.js and `npm` (for the dashboard)
- A Kaggle account (for the two source datasets)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An OpenRouter or Mistral API key (OpenRouter has a usable free tier; the engine tries
  it first and falls back to Mistral if only that key is set)
- A Supabase project
- A MongoDB connection string (Atlas free tier works)
- A way to expose `localhost:8000` publicly for Telegram's webhook. This project used
  [`cloudflared`](https://github.com/cloudflare/cloudflared)'s free, account-less quick
  tunnel, no sign-up needed

### 1. Configure environment variables

```bash
cp .env.example .env
```

Fill in `.env`. `.env.example` documents the baseline set:

- `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD`: Odoo connection (defaults work
  for the local Docker setup below: `http://localhost:8069`, `leadgate`, `admin`/`admin`)
- `OPENROUTER_API_KEY` and/or `MISTRAL_API_KEY`: at least one is required
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`: the second is a secret you generate
  yourself (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`) and
  register with Telegram in Step 8 below
- `ACTIVE_DOMAIN`: `cars` or `real_estate`, picks which adapter the engine loads
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`: from your Supabase project's API settings
- `SUPABASE_ANON_KEY`: the dashboard's public anon key, also from your Supabase
  project's API settings (used in Step 9, never the service key)
- `SUPABASE_DB_PASSWORD` or `SUPABASE_ACCESS_TOKEN`: fill in one of these two (leave the
  other as the placeholder). Needed once, to create the Supabase table in Step 6, since
  the service key alone can't run DDL (see that step for where to get either one)
- `MONGODB_URI`: your MongoDB connection string
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`: the Odoo Postgres container's
  credentials (defaults are fine for local dev)
- `KAGGLE_USERNAME`, `KAGGLE_KEY`: from your Kaggle account settings, needed only for
  Step 5's dataset download
- `GENERIC_TIMEZONE`, `N8N_OWNER_EMAIL`, `N8N_OWNER_PASSWORD`: n8n's timezone and the
  owner account you'll create in Step 6
- `N8N_API_KEY`: `.env.example` carries this as a placeholder like everything else, but
  it can't actually be filled in until n8n is already running. Generate it from the n8n
  UI (Settings > n8n API) after Step 2 (n8n's container is up by then), then add the real
  value to `.env` before running the n8n scripts in Step 6

### 2. Start the core services

```bash
docker compose up -d
```

This brings up Postgres, Odoo 18, and n8n, all three bound to `127.0.0.1` only (see the
comments in `docker-compose.yml` for why each one is loopback-only rather than exposed).

### 3. Create the Odoo database and install modules

Odoo's XML-RPC `db.create_database` call doesn't work cleanly against a single
just-started Postgres container (it auto-routes to the wrong, uninitialized database),
so this project created the database via the Odoo CLI directly:

```bash
docker compose exec -T odoo odoo -d leadgate --db_host=postgres --db_port=5432 \
  --db_user=odoo --db_password=odoo -i base --stop-after-init --without-demo=all

docker compose exec -T odoo odoo -d leadgate --db_host=postgres --db_port=5432 \
  --db_user=odoo --db_password=odoo -i crm,sale_management,stock,base_automation \
  --stop-after-init
```

Then install this project's own module, `leadgate_domain` (the domain-agnostic
`leadgate.catalog.item` model that both adapters query), from `odoo/addons/`, which is
already mounted into the container by `docker-compose.yml`:

```bash
docker compose exec odoo odoo -d leadgate --db_host=postgres --db_user=odoo \
  --db_password=odoo -i leadgate_domain --stop-after-init
```

The CLI-created database has no usable admin password yet. Set one via `odoo shell`
(open a Python shell against the running container and run):

```python
user = env['res.users'].browse(2)
user.write({'login': 'admin', 'password': 'admin', 'email': 'admin@leadgate.local'})
env.cr.commit()
```

Confirm Odoo is reachable at `http://localhost:8069` and you can log in with
`admin`/`admin` (or whatever you set).

### 4. Install Python dependencies

The next two steps (loading the catalog data and wiring up the n8n sync pipeline) run
scripts that need `pandas`, `kaggle`, and `psycopg2-binary`, so the Python environment has
to exist before those steps, not after them:

```bash
python -m venv .venv
.venv/Scripts/activate        # .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

### 5. Load the catalog data

Two Kaggle datasets are downloaded, cleaned, and normalized, then loaded into Odoo:

```bash
python data/prepare_cars.py           # downloads rebrowser/autotrader-dataset, outputs data/cars_raw.csv
python data/prepare_real_estate.py    # downloads ahmedshahriarsakib/usa-real-estate-dataset, outputs data/real_estate_raw.csv
python data/seed_odoo.py              # bulk-creates leadgate.catalog.item records from both CSVs via XML-RPC
```

`seed_odoo.py` reads `ODOO_URL`/`ODOO_DB`/`ODOO_USER`/`ODOO_PASSWORD` from `.env` and
creates one record per CSV row (`domain_type="cars"` or `"real_estate"`), with every
column besides `price` packed into a JSON `attributes` field.

### 6. Wire up the n8n sync pipeline

n8n's own first-run setup requires an owner account before anything else works:

```bash
curl -X POST http://localhost:5678/rest/owner/setup \
  -H "Content-Type: application/json" \
  -d '{"email":"<N8N_OWNER_EMAIL>","firstName":"LeadGate","lastName":"Admin","password":"<N8N_OWNER_PASSWORD>"}'
```

Then, from the n8n UI (Settings > n8n API), generate an API key and replace the
`N8N_API_KEY` placeholder in `.env` with the real value (it can't be filled in until n8n
is already running, which is why it's a placeholder at this point rather than a real
value from Step 1). With that in place:

```bash
python n8n/scripts/setup_n8n_credentials.py   # creates the Supabase + MongoDB credentials n8n needs
python n8n/scripts/deploy_workflow.py         # deploys and activates n8n/workflows/odoo-sync.json
python n8n/scripts/set_odoo_webhook_param.py  # points Odoo's sync webhook at the running n8n workflow
```

Before the first script above will do anything useful, the Supabase destination table
has to exist. `SUPABASE_URL` plus `SUPABASE_SERVICE_KEY` alone cannot create it:
Supabase's REST API (PostgREST) has no DDL endpoint by design. Fill in one of the two
placeholders already in `.env` (leave the other one as-is), then run the table-creation
script:

- `SUPABASE_DB_PASSWORD`: the project's direct Postgres password (Supabase dashboard >
  Project Settings > Database > Connection string)
- `SUPABASE_ACCESS_TOKEN`: a personal access token (starts with `sbp_`, from Account >
  Access Tokens)

```bash
python n8n/scripts/setup_supabase_table.py
```

This runs the DDL in `n8n/scripts/setup_supabase_table.sql`, creating
`public.catalog_items`.

### 7. Run the engine

The Python environment was already set up in Step 4, so this is just:

```bash
uvicorn engine.main:app
```

The engine starts on `http://localhost:8000`. `GET /health` should return
`{"status": "ok"}`.

### 8. Expose the webhook and register it with Telegram

```bash
cloudflared tunnel --url http://localhost:8000
```

This prints a public HTTPS URL (a free, no-account "quick tunnel":
`https://<random-words>.trycloudflare.com`). Register it as your bot's webhook:

```bash
curl -F "url=<TUNNEL_URL>/webhook/telegram" \
     -F "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
     https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
```

Message your bot on Telegram. You should see the request land in the FastAPI logs and
get a reply back.

### 9. Run the dashboard

```bash
cd dashboard
npm install
```

Create `dashboard/.env.local` (this is a separate, browser-facing env file: Vite only
exposes variables prefixed `VITE_`, and only the public anon key belongs here, never the
service key). Reuse the same `SUPABASE_URL` and `SUPABASE_ANON_KEY` values already in
your root `.env`:

```
VITE_SUPABASE_URL=<your SUPABASE_URL>
VITE_SUPABASE_ANON_KEY=<your SUPABASE_ANON_KEY>
```

```bash
npm run dev
```

The dashboard opens on `http://localhost:5173` and subscribes to `catalog_items` over
Supabase Realtime, so changing a record's status in Odoo (or via the eval/seed scripts)
should appear there within a second or two.

### 10. Reproduce the evaluation

```bash
.venv/Scripts/python.exe eval/run_eval.py --domain cars
.venv/Scripts/python.exe eval/run_eval.py --domain real_estate
.venv/Scripts/python.exe eval/metrics.py
```

`run_eval.py` sends every hand-labeled test case through the real engine, the real LLM,
and the real Odoo adapters end to end (no mocking) and writes the transcripts to
`eval/results/`. `metrics.py` scores those transcripts and prints the table above. See
[`eval/REPORT.md`](eval/REPORT.md) for what each metric means and the full debugging
history behind these numbers.

## Security

See [`SECURITY.md`](SECURITY.md) for what's covered and what a formal review would
still flag before this went anywhere near real customer data.

## Project structure

```
LeadGate/
├── engine/                  # FastAPI app: webhook, agent loop, LLM client, Odoo client
│   ├── core/                # Domain-agnostic agent loop + adapter interface
│   └── adapters/             # cars.py, real_estate.py: the only domain-specific code
├── odoo/addons/leadgate_domain/  # Custom Odoo module: catalog model + sync webhook
├── data/                    # Dataset prep scripts + seeding script
├── n8n/                     # Sync workflow + deployment/setup scripts
├── dashboard/               # React + Supabase Realtime dashboard
├── eval/                    # Evaluation harness, datasets, results, REPORT.md
└── docker-compose.yml       # Postgres, Odoo, n8n (all loopback-only)
```
