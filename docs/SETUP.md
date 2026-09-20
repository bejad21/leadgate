# LeadGate setup guide

Full, ordered setup for running LeadGate locally. Every command here comes from the real
scripts in this repo and was run in this order to build the working system. For what the
project is and what it looks like running, see the [README](../README.md).

## Prerequisites

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

## 1. Configure environment variables

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
- `TELEGRAM_ALERTS_BOT_TOKEN`, `TELEGRAM_ALERTS_CHAT_ID` (optional): a second bot that
  messages only you when a lead arrives (see "Alerts" under Step 6)
- `TELEGRAM_ALERTS_WEBHOOK_SECRET` (optional): a secret you generate yourself. Telegram
  sends it with every update from the alert bot, and the engine refuses updates without
  it. You need it for the alert buttons and for replying to customers (see "Buttons and
  replies" under Step 8)
- `ODOO_SALESPERSON_LOGIN` (optional): the Odoo login that new leads are assigned to.
  Defaults to `ODOO_USER`
- `DEFAULT_PHONE_COUNTRY_CODE` (optional): the country code of your customers, digits only.
  A phone number the assistant records must be one the customer typed, or that number with
  this code in front. The default is 971
- `LEAD_REMINDER_MINUTES` (optional): how long an untouched lead waits before the owner
  gets one reminder. The default is 30; 0 turns reminders off
- `DASHBOARD_LOGIN_EMAIL`, `DASHBOARD_LOGIN_PASSWORD`: the staff login for the dashboard's
  Leads tab, created in Step 6
- `CHAT_REF_SECRET`: any long random string. It keys the hash that stands in for a Telegram
  chat id in Supabase. Without it, conversations are not mirrored to the dashboard
- `N8N_API_KEY`: `.env.example` carries this as a placeholder like everything else, but
  it can't actually be filled in until n8n is already running. Generate it from the n8n
  UI (Settings > n8n API) after Step 2 (n8n's container is up by then), then add the real
  value to `.env` before running the n8n scripts in Step 6

## 2. Start the core services

```bash
docker compose up -d
```

This brings up Postgres, Odoo 18, and n8n, all three bound to `127.0.0.1` only (see the
comments in `docker-compose.yml` for why each one is loopback-only rather than exposed).

## 3. Create the Odoo database and install modules

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

## 4. Install Python dependencies

The next two steps (loading the catalog data and wiring up the n8n sync pipeline) run
scripts that need `pandas`, `kaggle`, and `psycopg2-binary`, so the Python environment has
to exist before those steps, not after them:

```bash
python -m venv .venv
.venv/Scripts/activate        # .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

## 5. Load the catalog data

Two Kaggle datasets are downloaded, cleaned, and normalized, then loaded into Odoo:

```bash
python data/prepare_cars.py           # downloads rebrowser/autotrader-dataset, outputs data/cars_raw.csv
python data/prepare_real_estate.py    # downloads ahmedshahriarsakib/usa-real-estate-dataset, outputs data/real_estate_raw.csv
python data/seed_odoo.py              # bulk-creates leadgate.catalog.item records from both CSVs via XML-RPC
```

`seed_odoo.py` reads `ODOO_URL`/`ODOO_DB`/`ODOO_USER`/`ODOO_PASSWORD` from `.env` and
creates one record per CSV row (`domain_type="cars"` or `"real_estate"`), with every
column besides `price` packed into a JSON `attributes` field.

## 6. Wire up the n8n sync pipeline

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

This runs `n8n/scripts/setup_supabase_table.sql`, which creates `public.catalog_items`,
turns on Row Level Security with a read-only policy for the public anon key, and adds the
table to Supabase's `supabase_realtime` publication. That last part matters: a new table
is not in the publication by default, and without it the dashboard connects but never
receives live updates. The script is safe to re-run.

The n8n workflow only mirrors an item when its status changes, so the new table starts
empty and the dashboard would show only items that have already changed. Copy the whole
catalog across once (safe to re-run; it upserts on `odoo_id`):

```bash
python n8n/scripts/backfill_supabase.py
```

Leads and conversations live in two more tables that are private. Only accounts marked
as staff can read them, and being signed in is not enough on its own, because Supabase
lets anyone register unless sign-ups are turned off. Set `DASHBOARD_LOGIN_EMAIL`,
`DASHBOARD_LOGIN_PASSWORD` and `CHAT_REF_SECRET` in `.env` (see `.env.example`), then:

```bash
python n8n/scripts/setup_supabase_table.py setup_supabase_leads.sql   # the tables, RLS, realtime
python n8n/scripts/create_dashboard_user.py                            # your staff login
python n8n/scripts/verify_leads_rls.py                                 # who can read what
```

`verify_leads_rls.py` plants a probe row and checks that an anonymous visitor and a
signed-in stranger see nothing while you see everything, that even you cannot write with
the browser key, and that a signed-in user can still see the public catalog. All eight
checks should pass. The SQL is safe to re-run on an existing install: it adds the `kind`,
`status` and `detail` columns to `leads` if they are missing.
It is also worth turning off "Allow new users to sign up" in the Supabase dashboard
(Authentication, Sign In / Providers), since nobody else needs an account.

### Alerts (optional)

To be told when a lead arrives, create a second bot so alerts never share a token with
the customer bot: in Telegram open @BotFather, send `/newbot`, name it "LeadGate Alerts",
and pick a username ending in `bot`. Open the new bot and send it `/start` (a bot can
only message someone who has messaged it first). Then find your chat id:

```bash
curl "https://api.telegram.org/bot<ALERT_BOT_TOKEN>/getUpdates"
```

Copy the number after `"chat":{"id":` and put both values in `.env` as
`TELEGRAM_ALERTS_BOT_TOKEN` and `TELEGRAM_ALERTS_CHAT_ID`. If either is missing the engine
simply skips alerts.

The alert ends with an "Open in Odoo" link built from `ODOO_PUBLIC_URL`. Telegram does not
turn `localhost` addresses into links, so while Odoo runs only on your machine the text
shows but is not tappable. Set `ODOO_PUBLIC_URL` to an address your phone can reach and the
link works.

### The catalog inside Odoo

The `leadgate_domain` module adds a **LeadGate > Catalog** menu to Odoo: a list with
coloured status badges, filters, and a form with a clickable Available / Reserved / Sold
bar. Change a status there and the dashboard updates.

The module also handles holds. When the assistant holds an item for a customer, Odoo
checks and records it in one transaction: an item can only be held once, one customer can
hold one item, and no more than ten holds can be active at a time (change the limit with
the system parameter `leadgate.max_active_holds`). A scheduled job runs every five minutes
and puts lapsed holds back on the board. Both the hold and the release are status changes,
so they reach the dashboard through n8n like any other. After pulling this change into an
existing install, upgrade the module:

```bash
docker compose exec odoo odoo -d leadgate --db_host=postgres --db_user=odoo \
  --db_password=odoo -u leadgate_domain --stop-after-init
docker compose restart odoo
```

## 7. Run the engine

The Python environment was already set up in Step 4, so this is just:

```bash
uvicorn engine.main:app
```

The engine starts on `http://localhost:8000`. `GET /health` should return
`{"status": "ok"}`.

## 8. Expose the webhook and register it with Telegram

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

### Buttons and replies

The alert bot's buttons and replies reach the engine through a webhook of their own. The
customer bot's webhook does not cover it, because it is a different bot. With the engine
running and the tunnel up, set `TELEGRAM_ALERTS_WEBHOOK_SECRET` in `.env`, restart the
engine, and register the alert bot:

```bash
python -m engine.register_alert_webhook https://<random-words>.trycloudflare.com
```

Press a button under an alert, or reply to one, and the engine handles it. Only updates
from your own chat are acted on. `python -m engine.register_alert_webhook --remove` takes
the webhook off again. A quick tunnel gets a new address every time it restarts, so run
the command again after restarting it.

## 9. Run the dashboard

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
should appear there within a second or two: the tag swings on its hook, the tallies
move, and a stamped row lands on the sign-out sheet. If the board is empty, run the
backfill in step 6. Open `#/leads` (the Leads tab) and sign in with your staff login to see
leads and conversations. See [dashboard/README.md](../dashboard/README.md) for how it works.

### Hosting the dashboard

The dashboard is a static app, so GitHub Pages can host it for free. It talks to Supabase
with the public anon key only. Under Settings > Secrets and variables > Actions add the
variables `SUPABASE_URL` and `SUPABASE_ANON_KEY`, set Settings > Pages > Source to
"GitHub Actions", and run the "Deploy dashboard" workflow. Visitors see the live key board
and sample conversations; real leads stay behind the staff sign-in.

## 10. Reproduce the evaluation

```bash
.venv/Scripts/python.exe eval/run_eval.py --domain cars
.venv/Scripts/python.exe eval/run_eval.py --domain real_estate
.venv/Scripts/python.exe eval/metrics.py
```

`run_eval.py` sends every hand-labeled test case through the real engine, the real LLM,
and the real Odoo adapters end to end (no mocking) and writes the transcripts to
`eval/results/`. `metrics.py` scores those transcripts and prints the table above. See
[`eval/REPORT.md`](../eval/REPORT.md) for what each metric means and the full debugging
history behind these numbers.
