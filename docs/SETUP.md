# LeadGate setup guide

Full, ordered setup for running LeadGate locally. For what the
project is and what it looks like running, see the [README](../README.md).

## Prerequisites

- Docker and Docker Compose
- Python 3.11+ and `pip`
- Node.js and `npm` (for the dashboard)
- A Kaggle account (for the two source datasets)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An API key from at least one model provider: NVIDIA ([build.nvidia.com](https://build.nvidia.com),
  free for development; its default model is the one the published results used), OpenRouter (free
  tier) or Mistral. With several keys the engine tries NVIDIA first, then OpenRouter's free models
  in order, then Mistral
- A Supabase project (the free tier works): it holds the dashboard's data and the engine's own durable state
- A way to expose `localhost:8000` publicly for Telegram's webhook. [`cloudflared`](https://github.com/cloudflare/cloudflared)'s free, account-less quick
  tunnel works, no sign-up needed

## 1. Configure environment variables

```bash
cp .env.example .env
```

Fill in `.env`. `.env.example` documents the baseline set:

- `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD`: Odoo connection (defaults work
  for the local Docker setup below: `http://localhost:8069`, `leadgate`, `admin`/`admin`)
- `ODOO_TIMEOUT_SECONDS` (optional): how long a call to Odoo may wait before it fails.
  Defaults to 30
- `NVIDIA_API_KEY`, `OPENROUTER_API_KEY` and/or `MISTRAL_API_KEY`: at least one is required; leave the
  ones you do not use blank. A provider that is retired, rate limited or failing hands over to the next,
  but a rejected key (401 or 403) does not. NVIDIA's free queue can be slow, so set
  `LLM_TIMEOUT_SECONDS=120` with it
- `LLM_REASONING_EFFORT` (optional: `low`, `medium` or `high`; sent only when set): the published
  results used `low`. Without it a thinking model such as `mistral-large-4` can spend the whole token
  budget thinking. It is sent to every configured provider
- `MISTRAL_MODEL`, `NVIDIA_MODEL` (optional): pin the model each of those providers uses
- `LLM_TIMEOUT_SECONDS` (optional, 1 to 600, default 30): how long one model call may take
- `LLM_TEMPERATURE` (optional, 0 to 2, default 0), `LLM_MAX_TOKENS` (optional, default 800) and
  `LLM_SEED` (optional, a whole number, sent only when set): sent with every model call. Temperature 0
  suits tool calling; not every provider honours a seed. A bad value stops the engine at startup and
  the error names the variable
- `LLM_PRICE_PER_MTOK_INPUT` and `LLM_PRICE_PER_MTOK_OUTPUT` (optional, dollars per million tokens,
  default 0): used to estimate a turn's cost when the provider does not report one. 0 is right for
  free models
- `LOG_LEVEL` (optional): `DEBUG`, `INFO` (default), `WARNING`, `ERROR` or `CRITICAL`
- `TRACE_FILE` (optional): a file that gets one JSON line per customer turn (see "Traces and logs" below)
- `OPENROUTER_MODELS` (optional): comma-separated OpenRouter model ids, tried in order. The
  default list is in `engine/config.py`. Free models are retired from time to time, so if
  replies stop with a 404 in the engine's log, pick current ones from
  `https://openrouter.ai/api/v1/models` (free models end in `:free`)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`: the second is a secret you generate (at least 16 characters; a shorter one stops the engine at startup)
  yourself (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`) and
  register with Telegram in Step 8 below
- `ACTIVE_DOMAIN`: `cars` or `real_estate`, picks which adapter the engine loads (any other value stops the engine at startup)
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`: from your Supabase project's API settings. The URL is
  the project address (`https://<ref>.supabase.co`; a trailing `/rest/v1` is accepted and removed).
  The engine keeps chat history, the lead-to-chat map, handled update ids and rate-limit counters
  here, in tables made by Step 6. Without these two it keeps them in memory and forgets them on restart
- `SUPABASE_ANON_KEY`: the dashboard's public anon key, also from your Supabase
  project's API settings (used in Step 9, never the service key)
- `SUPABASE_DB_PASSWORD`: the project's Postgres password (Project Settings > Database). Needed
  to create the Supabase tables in Step 6, since the service key alone can't run DDL
- `ODOO_WEBHOOK_SECRET`: a secret you generate yourself, at least 16 characters (for example
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`). Odoo sends it with every status
  change and the engine refuses calls without it; Step 6 gives it to Odoo. Until it is set,
  `POST /webhook/odoo-status` answers 503 and does nothing
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`: the Odoo Postgres container's
  credentials. All three are required: `docker compose` stops with an error if one is
  missing, so there is no weak default password to forget about. Pick any values you
  like for local use, and use the same ones in the commands in Step 3
- `KAGGLE_USERNAME`, `KAGGLE_KEY`: from your Kaggle account settings, needed only for
  Step 5's dataset download
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
- `DASHBOARD_LOGIN_EMAIL`, `DASHBOARD_LOGIN_PASSWORD`: the staff login for the dashboard's
  Leads tab, created in Step 6
- `CHAT_REF_SECRET`: any long random string. It keys the hash that stands in for a Telegram
  chat id in Supabase. Without it, conversations are not mirrored to the dashboard

## 2. Start the core services

```bash
docker compose up -d
```

This brings up Postgres and Odoo 18, both bound to `127.0.0.1` only (see the
comments in `docker-compose.yml` for why each one is loopback-only rather than exposed).
The image versions are pinned (Odoo by digest, because its `18.0` tag is rebuilt in place).
The engine is not part of this command: run it on the host or in Docker (both in Step 7).

## 3. Create the Odoo database and install modules

Odoo's XML-RPC `db.create_database` call doesn't work cleanly against a single
just-started Postgres container (it auto-routes to the wrong, uninitialized database),
so create it with the Odoo CLI directly:

```bash
docker compose exec -T odoo odoo -d leadgate --db_host=postgres --db_port=5432 \
  --db_user=<POSTGRES_USER> --db_password=<POSTGRES_PASSWORD> -i base --stop-after-init --without-demo=all

docker compose exec -T odoo odoo -d leadgate --db_host=postgres --db_port=5432 \
  --db_user=<POSTGRES_USER> --db_password=<POSTGRES_PASSWORD> -i crm,sale_management,stock,base_automation \
  --stop-after-init
```

Then install this project's own module, `leadgate_domain` (the domain-agnostic
`leadgate.catalog.item` model that both adapters query), from `odoo/addons/`, which is
already mounted into the container by `docker-compose.yml`:

```bash
docker compose exec odoo odoo -d leadgate --db_host=postgres --db_user=<POSTGRES_USER> \
  --db_password=<POSTGRES_PASSWORD> -i leadgate_domain --stop-after-init
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

The engine needs the pinned runtime packages in `requirements.txt`. Create the virtualenv
now, because the next steps run scripts that need more than the engine does:

```bash
python -m venv .venv
source .venv/Scripts/activate     # Git Bash on Windows
# .venv\Scripts\activate        # PowerShell or cmd on Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
pip install -r requirements-tools.txt   # needed from Step 5 on (data preparation, Supabase scripts)
```

Three files, all pinned to the versions the tests ran against (`pyproject.toml` holds the
ranges they were generated from):

| File | Install it when |
|---|---|
| `requirements.txt` | Always. It is all the engine needs to run. |
| `requirements-tools.txt` | Before Step 5 and the Supabase scripts in Step 6: `pandas`, `kaggle`, `psycopg2-binary`, `matplotlib` and `playwright`. It includes `requirements.txt`. |
| `requirements-dev.txt` | To run the tests or the linters ([Run the tests](#run-the-tests)). It includes `requirements.txt`. |

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

## 6. Create the Supabase tables and connect Odoo to the engine

Supabase's REST API (PostgREST) has no DDL endpoint by design, so the tables are created by connecting
to the project's Postgres directly. Put the project's database password in `.env` as
`SUPABASE_DB_PASSWORD` (Supabase dashboard > Project Settings > Database), then:

```bash
python scripts/apply_migrations.py            # apply every file in migrations/ that is not applied yet
python scripts/apply_migrations.py --status   # list applied and pending files, change nothing
```

The files in `migrations/` run in order, once each, and are recorded in `public.schema_migrations` with a
checksum. Run it again and it applies only new files; a file edited after it was applied stops the run, so a
change always goes into a new numbered file. Each file runs in one transaction. They are also written to be
safe on a project that already has the tables. They create:

- `0001`: `public.catalog_items`, the table the dashboard's key board reads, with Row Level Security (a
  read-only policy for the anon and authenticated roles) and a place in Supabase's `supabase_realtime`
  publication. A new table is not in the publication by default, and without it the dashboard connects but
  never receives live updates.
- `0002`: `public.leads` and `public.conversation_turns`, private tables for the dashboard's Leads tab.
- `0003`: the engine's own state: `chat_turns`, `lead_chats`, `alert_messages`, `processed_updates`,
  `rate_limits`, `lead_flags`, and the two functions that count a rate-limit hit and purge old rows. Row Level
  Security is on, no policy exists, and the browser roles are denied outright: only the engine's service key
  reads or writes them.
- `0004`: `public.apply_catalog_event`, the function the status webhook calls (below).
- `0005`: a fix to the rate-limit function from `0003`, which Postgres refused to run because a variable and a
  column had the same name.

### How a status change reaches the dashboard

When an item's status changes in Odoo (by hand, by a hold, by the job that releases lapsed holds), an
automated action in the `leadgate_domain` module posts the item's id, domain, name, price, new status and
write time to the engine's `POST /webhook/odoo-status`, with `ODOO_WEBHOOK_SECRET` in the
`X-Odoo-Webhook-Secret` header. The engine compares the secret in constant time before it reads the body,
caps the body at 8 KiB, validates the fields, answers `202` at once (Odoo makes this call inside its own
write and gives up after 5 seconds) and then upserts the `catalog_items` row. The upsert is one database
function, so a replayed event changes nothing, an event older than the row's stored write time is ignored,
and two simultaneous deliveries make one row. Supabase errors are retried with a growing pause (four
attempts); if they all fail the event is logged by item id and dropped, and `scripts/backfill_supabase.py`
resyncs. The row's `updated_at` is Odoo's write time.

Give Odoo the address and the secret (the engine's name on the Compose network, because Odoo calls it from
inside its container):

```bash
python scripts/set_odoo_webhook_param.py   # sets leadgate.status_webhook_url and leadgate.status_webhook_secret
```

After changing the Python in `odoo/addons`, restart the Odoo container so it loads the new code:
`docker compose restart odoo`. To check the whole path with the engine and Odoo running:

```bash
python scripts/verify_sync.py   # flips one cars item in Odoo and waits for Supabase to show it, then restores it
```

The status webhook only mirrors an item when its status changes, so the new table starts
empty and the dashboard would show only items that have already changed. Copy the whole
catalog across once (safe to re-run; it upserts on `odoo_id`):

```bash
python scripts/backfill_supabase.py
```

Leads and conversations live in two more tables that are private. Only accounts marked
as staff can read them, and being signed in is not enough on its own, because Supabase
lets anyone register unless sign-ups are turned off. Set `DASHBOARD_LOGIN_EMAIL`,
`DASHBOARD_LOGIN_PASSWORD` and `CHAT_REF_SECRET` in `.env` (see `.env.example`), then:

```bash
python scripts/create_dashboard_user.py   # your staff login
python scripts/verify_leads_rls.py        # who can read what
```

`verify_leads_rls.py` plants a probe row and checks that an anonymous visitor and a
signed-in stranger see nothing while you see everything, that even you cannot write with
the browser key, that a signed-in user can still see the public catalog, and that none of the engine's own
tables or functions can be reached with a browser key.
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
and makes items with lapsed holds available again. Both the hold and the release are status changes,
so they reach the dashboard through the status webhook like any other. On an
existing install, upgrade the module after updating:

```bash
docker compose exec odoo odoo -d leadgate --db_host=postgres --db_user=<POSTGRES_USER> \
  --db_password=<POSTGRES_PASSWORD> -u leadgate_domain --stop-after-init
docker compose restart odoo
```

## 7. Run the engine

The Python environment was already set up in Step 4, so this is just:

```bash
uvicorn engine.main:app
```

The engine starts on `http://localhost:8000`. `GET /health` should return
`{"status": "ok"}`.

`GET /ready` is the readiness probe: it checks that Odoo answers (on the engine's cached login: a probe
logs in only if no login has succeeded yet, and after a failed login the engine remembers the
failure for 10 seconds and answers at once instead of trying again, so a probe cannot stack up slow
logins), that the store responds (a read of the Supabase store, always
fine for the in-memory store) and that an LLM key and a model are configured. It does not call the
model. It answers `200` with `{"status": "ready", "checks": {...}}`, or `503` with `"not_ready"` and a
short reason for each failed check (`unreachable`, `login rejected`, `timed out after 3s`, never a URL
or a credential). Nothing is cached, so a dependency that comes back shows up on the next probe. The
Docker `HEALTHCHECK` stays on `/health`: Odoo takes a while to start, and `/ready` is rightly `503`
until it answers, which would mark the container unhealthy for no good reason. Point a load balancer or
a monitor at `/ready`.
FastAPI's interactive API pages (`/docs`, `/redoc` and `/openapi.json`) are switched off by
design, because step 8 puts the engine on a public URL.

### Or run the engine in Docker

Instead of running `uvicorn` on the host, the same Compose project can run the engine:

```bash
docker compose up -d engine
docker compose logs -f engine
```

This builds the image from the `Dockerfile` (the pinned `requirements.txt` only, run as an
unprivileged user), starts it beside Odoo, and publishes it on `127.0.0.1:8000`, so step 8 works
unchanged. It reads the same `.env`, but three values mean something different inside the container:

- `ODOO_URL` is set to `http://odoo:8069`, because inside the Compose network Odoo is reached by its
  service name, not by `localhost`.
- Odoo reaches the engine at `http://engine:8000` on the Compose network, which is where
  `scripts/set_odoo_webhook_param.py` points it. Start the engine in Docker for the status
  webhook to work from Odoo; if you run `uvicorn` on the host, set `ODOO_STATUS_WEBHOOK_URL` to an address
  Odoo's container can reach (for example `http://host.docker.internal:8000/webhook/odoo-status`).
- `TRACE_FILE` must be a path the container's unprivileged user can write. A relative path such as
  `./traces/turns.jsonl` lands in `/app`, which that user cannot write to. Use a path like
  `/tmp/traces/turns.jsonl` (lost when the container is recreated) or mount a volume and point
  `TRACE_FILE` into it. If the file cannot be written, the engine logs a warning at startup (and again on each turn it cannot write) and
  traces then go to the log only.

The container reports itself healthy once `GET /health` answers, and restarts
unless you stop it. After a code change, rebuild with `docker compose up -d --build engine`.
Do not run it together with a host `uvicorn`: both want port 8000, which is why a plain
`docker compose up -d` does not start it.

### Traces and logs

Every customer turn produces one trace: which model answered each call, tokens, cost, how long the turn
and the model took, how each tool call ended (`ok`, `validation_error`, `limit`, `execution_error`,
`duplicate`), whether a retry round or a failover happened, and the outcome (`reply`, `blocked`,
`error`). It goes to the `leadgate.trace` log line and, when `TRACE_FILE` is set, is also
appended to that file as JSON lines. Each line carries the update id and `chat_ref`, a keyed hash of the
chat (HMAC with `CHAT_REF_SECRET`; without the secret it is a plain truncated hash, so set the secret
before traces leave your machine). Message text, replies, tool arguments, names and contact details
never appear in a trace.

Read a trace file with:

```bash
python eval/trace_summary.py path/to/traces.jsonl
```

It prints the number of turns, mean and 95th percentile latency, mean tokens per turn, total and mean
cost, the share of turns that used a failover and a retry round, and the cost per lead (the total cost
of the turns in which `create_lead` succeeded, divided by their number). When the traces carry a
`chat_ref` it also prints conversation figures: the share of chats that stopped after one reply or
reached a request, how often a person was asked for, and how often a button was tapped. `eval/run_eval.py` writes the
same traces, so a smoke run (`EVAL_RECORD_LEADS=1 TRACE_FILE=... python eval/run_eval.py --domain cars
--limit 3`) gives the figures without touching `eval/results`: a `--limit` run writes its partial
results to a temp folder (it says where) and refuses `--results-dir eval/results`.

Logs are one JSON object per line (timestamp, level, logger, message, exception text, and the
`update_id` and `chat_ref` of the request being handled). Telegram bot tokens, bearer tokens and
`api_key=` values are masked, and the `httpx` and `httpcore` loggers stay at `WARNING` because their
`INFO` lines would print request URLs, which contain the bot token.

## 8. Expose the webhook and register it with Telegram

```bash
cloudflared tunnel --url http://localhost:8000
```

This prints a public HTTPS URL (a free, no-account "quick tunnel":
`https://<random-words>.trycloudflare.com`). Register it as your bot's webhook:

```bash
curl -F "url=<TUNNEL_URL>/webhook/telegram" \
     -F "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
     -F 'allowed_updates=["message","callback_query"]' \
     https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
```

`callback_query` is what a tap on a button under a list of results arrives as. A webhook that was registered without it
(an older setup) still works for typed messages, but the buttons do nothing until you register it again with the line above.

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

Press a button under an alert, or reply to one, and the engine handles it. Only updates from your own chat are acted on. `python -m engine.register_alert_webhook --remove` takes
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

## 10. Reproduce the evaluation

The stack from the earlier steps must be running (Odoo with the seeded catalog) and `.env` must hold a
model key. The latest results used NVIDIA's host. Use one provider and one model so a failover cannot mix
models in a run:

```bash
export OPENROUTER_API_KEY= MISTRAL_API_KEY=                        # blank for this shell, so only NVIDIA is used
export NVIDIA_MODEL=mistralai/mistral-large-4-1t-a52b              # one model; its name is in every record
export LLM_REASONING_EFFORT=low LLM_TIMEOUT_SECONDS=120            # low effort as in the published runs; NVIDIA is slow
export EVAL_RECORD_LEADS=1                                         # holds and leads are recorded, not executed
R=.venv/Scripts/python.exe

# held-out (frozen) and dev, 5 repeats each; resumable: run the same command again to continue
$R -m eval.run_eval --domain cars --set heldout --repeats 5
$R -m eval.run_eval --domain real_estate --set heldout --repeats 5
$R -m eval.run_eval --domain cars --set dev --repeats 5
$R -m eval.run_eval --domain real_estate --set dev --repeats 5

# baselines on the held-out sets
$R -m eval.run_eval --domain cars --set heldout --repeats 5 --baseline router
$R -m eval.run_eval --domain cars --set heldout --repeats 5 --baseline notools

# red team: guardrails on, each one off alone (aimed attacks), and all off
$R -m eval.run_redteam --domain cars --set both --arm on --repeats 5
$R -m eval.run_redteam --domain cars --set both --arm off:tool_call_cap --targeted --repeats 5
$R -m eval.run_redteam --domain cars --set originals --arm all_off --repeats 5

# benign dev set with guardrails off (over-blocking check), then every table and chart
$R -m eval.run_eval --domain cars --set dev --repeats 1 --guardrails-off all
$R -m eval.report
```

Results go to `eval/results/<date>-<set>/` as JSON lines after a provenance header. `eval.report`
rewrites the tables in [`eval/REPORT.md`](../eval/REPORT.md) and the charts from those files only, and
refuses a file without a header. See that report for what each metric means and for the limits.

Expected live LLM calls (about two per agent case, one per no-tools case, none for the router):
held-out 36 + 30 cases x 5 repeats is about 660 calls; the dev sets (73 + 62 cases) x 5 about 1,350; each
no-tools baseline about 165; a red-team arm is up to 2 calls per non-blocked attack attempt. All counted calls
are written to `eval/results/call_ledger.json`, and `--max-calls` (default 4000) stops a run when reached.

Free-tier caveat: NVIDIA's free host and the free OpenRouter models have request limits that differ by
account and change without notice. The runner spaces calls (`--pace`, default 3 s), waits out a 429 for as long
as the provider says, and stops cleanly, marking the file incomplete, when the daily allowance is gone.
Run the same command the next day to continue. Latency and success vary between runs even at temperature 0.

## Run the tests

The engine suite needs no `.env`, no network and no running services. `conftest.py` replaces
every variable listed in `.env.example` (the commented optional ones too) with a harmless value, so a real `.env` can never point
a test at live Odoo, Supabase, the model API or Telegram.

```bash
pip install -r requirements-dev.txt
python -m pytest engine/tests -q
```

A bare `pytest` does the same (`pyproject.toml` limits it to `engine/tests`, so it does not try
to collect `dashboard/tests/leads_ui_test.py`, which is a Playwright script that needs the
dashboard and a staff login). On a clean checkout with `requirements-dev.txt` the result is all passed
plus 25 skipped (21 if `requirements-tools.txt` is installed too):

- 4 are the live-Odoo checks in `engine/tests/test_cars_adapter.py`: they call a running Odoo
  that holds the 350 seeded cars, and skip themselves when it is unreachable. Because
  `conftest.py` points `ODOO_URL` at `http://odoo.test`, they skip on every run; to run them, set
  `ODOO_URL`, `ODOO_DB`, `ODOO_USER` and `ODOO_PASSWORD` in `SAFE_VALUES` to your local Odoo for
  that one run, and do not commit the change.
- 17 are `engine/tests/test_store_contract.py` run against a real Supabase project. The same contract
  tests always run against the in-memory store and against `SupabaseStore` talking to an in-process
  stand-in for Supabase's REST interface. For a live run, apply the migrations to a project you can
  spare, then `LEADGATE_LIVE_STORE=1 python -m pytest engine/tests/test_store_contract.py` reads
  `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from `.env`. It writes rows under random negative ids and
  deletes them afterwards.
- 4 more need packages from `requirements-tools.txt`: three chart tests (`matplotlib`) and one migration
  test (`psycopg2`).

Coverage, from the same install: `python -m pytest --cov=engine`.

Lint and type checks, from the same install:

```bash
ruff check .
pyright
```

`.github/workflows/ci.yml` runs these, the tests with a coverage floor, the dashboard's lint, build
and tests, and an install of the `leadgate_domain` module into an empty Odoo 18 database. It runs on
GitHub for every pull request and every push to `main`.
