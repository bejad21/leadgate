# Security

This is a portfolio project, not a production deployment, but it was built with real
security decisions rather than none. This document says plainly what's actually covered,
what's deliberately out of scope, and what a formal review would still flag before this
went anywhere near real customer data. Nothing here is aspirational: every "covered"
item points at the actual code, and every gap below was checked, not guessed at.

## What's covered

**Webhook authentication is constant-time.** `POST /webhook/telegram`
(`engine/main.py`) checks `X-Telegram-Bot-Api-Secret-Token` with `hmac.compare_digest`,
not `!=`, so a network attacker timing responses can't recover the secret one character
at a time via early-exit string comparison. The check runs before the request body is
even parsed, so a malformed or oversized body can't cause a crash ahead of
authentication.

**Rate limiting and input caps.** A per-`chat_id` fixed-window limiter
(`engine/rate_limiter.py`) allows 10 requests per rolling 60-second window, tight enough
to stop a single chat from hammering the LLM/Odoo backends and running up API costs.
Incoming text is truncated at 2000 characters before it enters the LLM conversation
history, so one pathologically large message can't blow up context size or cost.

**Docker services are loopback-only.** Postgres (`5432`), Odoo (`8069`/`8072`), and n8n
(`5678`) are all bound to `127.0.0.1` in `docker-compose.yml`, not `0.0.0.0`. Postgres
has no auth hardening beyond a weak-by-default password, so it must never be reachable
from the LAN or internet. n8n's own first-run owner-account setup is unauthenticated
until an owner account is created, so exposing it before that completes would let anyone
who reaches the port first claim ownership. The only thing exposed to the public
internet at all is the FastAPI webhook, and only through a `cloudflared` tunnel that the
operator starts and stops manually. Nothing is publicly reachable by default.

**Secrets never enter the repo.** `.env` (real Odoo, LLM, Telegram, Supabase, MongoDB,
and n8n credentials) is gitignored and was never committed at any point across the
project's history, confirmed via `git status` before every commit in every task report.
`dashboard/.env.local` (the dashboard's Supabase anon key) is likewise gitignored. n8n's
credentials for Supabase and MongoDB are stored inside n8n itself, referenced from the
workflow by id/name, not embedded as literal values in the exported
`n8n/workflows/odoo-sync.json` (grep-verified clean of both the Supabase JWT prefix and
the MongoDB connection string format before that file was committed).

**Odoo access control is real, not default-open.** The custom `leadgate.catalog.item`
model ships its own `security/ir.model.access.csv` granting CRUD to `base.group_user`
specifically. This was added after Odoo's own install log warned the model had no
access rules at all, and confirmed by testing that even the admin login was rejected
with "no group currently allows this operation" until the rule was added. The model
isn't reachable by an anonymous or misconfigured group by accident.

**Webhook delivery failures can't corrupt data.** Odoo's `_notify_webhook()`
(`odoo/addons/leadgate_domain/models/lead_webhook.py`) catches `URLError`/`OSError`
around its outbound POST to n8n and logs a warning instead of raising. A dead or slow
n8n instance can never block or fail the Odoo record write that triggered it. n8n's own
workflow sets `onError: continueRegularOutput` on both the Supabase and MongoDB
branches, so a missing table or a bad connection string in one branch doesn't silently
prevent the other branch's insert from running (this was a real bug caught during Task
3.2/3.3, not a hypothetical).

**Least-privilege n8n credentials.** The n8n Supabase credential restricts
`allowedHttpRequestDomains` to that project's own Supabase host, not arbitrary URLs.
Even if the workflow logic were compromised, that credential couldn't be used to call
an attacker-controlled endpoint.

**Row Level Security is enabled on the dashboard's Supabase table.** The React
dashboard talks to Supabase using `VITE_SUPABASE_ANON_KEY`, the public, client-side key,
which is expected to be world-readable by design and is necessarily embedded in the
dashboard's client-side JavaScript bundle, so treating it as secret isn't an option.
What makes an anon key safe in a real Supabase deployment is Row Level Security (RLS)
policies on the underlying table restricting what that key can actually see or do.
`public.catalog_items` has RLS enabled (`n8n/scripts/setup_supabase_table.sql`, applied
live to the real project) with exactly one policy: a `for select ... to anon using
(true)` read-only policy. No insert/update/delete policy exists for `anon` or
`authenticated`, so with RLS on, those operations are denied by default for both roles;
only n8n's own `service_role` credential (which bypasses RLS entirely and is never
shipped to the browser) can still write. This was verified live, not just checked in the
DDL: querying `pg_class`/`pg_policy` directly confirms `relrowsecurity = true` and the
single `catalog_items_anon_read` policy, a real anon-key `GET` still returns rows
(`200`), and a real anon-key `POST` attempting to insert a row is rejected (`401`,
Postgres error `42501`, "new row violates row-level security policy").

## What a formal security review would still flag

**No data-residency guarantee from the free-tier LLM provider.** Every customer message
is sent to OpenRouter's free tier (`deepseek/deepseek-v4-flash-0731:free`), with Mistral
as a fallback. Neither the free-tier terms nor this project's configuration make any
data-residency, retention, or no-training guarantee. A real deployment handling actual
customer conversations would need a paid tier with an explicit data-processing agreement
before this is acceptable.

**Conversation history is in-memory and unencrypted, with no per-chat expiry.**
`conversation_history: dict[int, list[dict]]` in `engine/main.py` lives entirely in
process memory: it doesn't survive a restart, isn't shared across worker processes, and
is never encrypted at rest because it's never at rest, it's just a live dict. Each
chat's own turn history is capped at `CONVERSATION_HISTORY_MAX_TURNS` (20) to bound
per-request token cost and memory growth for any single conversation, but the
`chat_id` keys themselves are never evicted, so a process that talks to enough distinct
chats over a long enough uptime still grows unbounded, and there's no time-based expiry
for an idle chat's history. This is an explicit, commented trade-off in the code (a
production deployment would move this to Redis or Supabase with real TTLs), but as
shipped, every customer conversation this process has ever handled sits in plaintext
memory for as long as the process runs.

**No authentication on the eval or seed scripts' outputs.** `eval/results/*.json` and
the seeded catalog data are plain files with no access control beyond the filesystem;
this is fine for a portfolio artifact but would not be an acceptable pattern for a real
evaluation pipeline touching real customer transcripts.

**Single points of trust.** The Odoo `admin`/`admin` credentials used throughout local
setup are development defaults, documented as such, and never intended to survive
contact with a real deployment, but there is currently no automated check that stops
someone from running this in a less-trusted environment with those defaults still in
place.

**The engine authenticates to Odoo entirely as the `admin` service account.**
`engine/main.py`'s `_get_adapter()` builds a single `OdooClient` from
`ODOO_URL`/`ODOO_DB`/`ODOO_USER`/`ODOO_PASSWORD`, and every deployment documented in
this README configures `ODOO_USER=admin`. There is no separate, scoped service user
with just the permissions the engine actually needs (read/write on
`leadgate.catalog.item`, create on `crm.lead`); the engine's XML-RPC calls run with
full admin rights on the whole Odoo database, identical to a human logging into the
Odoo UI as `admin`. A compromise of the engine process, or a bug in either adapter that
lets a caller influence which model or method gets called, would carry the full blast
radius of an Odoo admin session, not a narrowly scoped one. A production deployment
should create a dedicated Odoo user with access rules restricted to exactly the models
and operations the two adapters use, and authenticate as that user instead.

**Untrusted customer text drives a tool-calling loop with a real write path.**
Every Telegram message reaching `run_turn()` (`engine/core/agent_loop.py`) is
attacker-controllable free text that gets sent to the LLM alongside the system prompt
and tool schemas, and the LLM's response can trigger `create_lead`, a real write that
creates a `crm.lead` record in Odoo. This is a prompt-injection surface: a customer
could try to craft a message designed to make the model call `create_lead` with
misleading arguments, or to make it ignore the system prompt's instructions. The
practical blast radius is narrower than a general-purpose agent, though: there are only
two hardcoded domains, each with exactly two fixed tool names and a fixed, small
argument schema (`search_inventory`/`search_listings` and `create_lead`); there is no
dynamic model name, table name, or arbitrary-code-execution path the model could steer
into, and the worst a successful injection could do through the exposed tools is create
a spurious `crm.lead` or shape a search's filter arguments. That said, this has not been
formally red-teamed with adversarial prompts, and "the tool surface is narrow" is a
mitigating factor, not proof the path is safe. A production deployment handling real
leads should add explicit adversarial testing of the prompt-injection surface before
trusting `create_lead`'s output unreviewed.

**Negation-blind free-text matching in the eval harness.** Not a production security
issue, but worth naming here since it was found during an adversarial review of this
project's own metrics: `eval/metrics.py`'s free-text matcher checks for word overlap
without checking for negation, so in principle a reply containing "not a cash buyer"
could score as matching an expected value of "cash buyer." No real case in the current
93-case suite triggers this, but it's a real, unfixed gap in the harness itself. See
`eval/REPORT.md` for the full account.
