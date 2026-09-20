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

**Prompt injection and tool abuse are screened in layers.** `engine/core/guardrails.py`
wraps the agent loop, and none of it depends on the model behaving.
1. Customer text is stripped of control and zero-width characters, Unicode look-alikes
   (NFKC) and chat-template tokens (`<|im_start|>`, `[INST]`, `<<SYS>>`) before anything
   else sees it. Messages that match known injection phrasings get a fixed refusal with
   no LLM call, no Odoo call, and no place in the conversation history.
2. The system prompt tells the model that customer text and tool results are data, not
   instructions, and forbids revealing itself, changing role, or sending links.
3. Every tool call the model makes is validated against that tool's own JSON schema:
   unknown tools and unknown arguments are dropped, numbers are range-checked, strings
   are trimmed to one line and length-capped, enums are enforced. A failed check becomes
   an error result and the adapter is never called. At most three tool calls run per turn.
4. `create_lead` is limited to three per chat per hour, and identical writes within one
   turn run once. The price on a lead is only recorded if a catalog item of that domain
   has it (`create_verified_lead` in `engine/core/adapter_base.py`); otherwise the lead
   is flagged and the model is told not to repeat the price. This proves the price
   exists in the catalog, not that it belongs to the item named on the lead, so a
   customer can still attach a real price from a different item.
5. The final reply has links removed (schemes, `mailto:`, `t.me`, and bare domains such as
   `evil.com/pay`, while email addresses are kept), is replaced if it repeats a chunk of
   the system prompt, and is never blank.

This is tested two ways. `engine/tests/test_compromised_model.py` scripts a model that
does everything an attacker wants in one turn (six leads at $1, an unknown tool, a
negative number, a link, a prompt leak) and checks the guardrails hold, then runs the
same attack with them off to show it succeeds. `eval/run_redteam.py` sends 26 adversarial
messages through the real webhook, LLM and Odoo reads, with pass criteria that are string
and structure checks instead of an LLM judge. Results are in `eval/REPORT.md`.

**Conversation history survives restarts.** The last 20 turns of a chat are kept in a
cache limited to 1000 chats (least recently used out) and rebuilt from the MongoDB
`conversations` collection whenever a chat is not in the cache, for example after a
restart. Stored turns are screened again on the way back in, so blocked turns, injections logged before the guardrails existed, and links are never replayed. A turn that fails is rolled back so a retry
does not stack duplicates, and Telegram redeliveries are ignored by `update_id`.

**Leads and conversations are private, and the database enforces it.** The `leads` and
`conversation_turns` tables (`n8n/scripts/setup_supabase_leads.sql`) have no policy for the
public anon key. Being signed in is not enough either, because Supabase lets anyone register
an account unless sign-ups are off, so `authenticated` is not a trusted group. Only users
whose server-set `app_metadata` says `role = 'staff'` can read, and a user cannot change
their own `app_metadata`. `n8n/scripts/verify_leads_rls.py` proves it from each visitor's
side: an anonymous visitor and a signed-in stranger see nothing, the owner sees everything,
and even the owner cannot write with the browser key. An independent code review found that
the first version of this policy trusted every signed-in user, which is why the check exists.
The engine writes with the service key, which never reaches the browser.

**Chats are stored under a keyed hash.** Supabase holds an HMAC of the Telegram chat id
(`CHAT_REF_SECRET`), not the id. Telegram ids are small numbers, so an unkeyed or
default-salted hash could be reversed by trying every id; the mirror refuses to write at all
if the secret is not set.

**Customer text is treated as hostile on the CRM path.** Contacts are parsed with strict
patterns (no wildcard characters, real top-level domains, dates are not phone numbers), a
partner is matched on the exact normalised email, names are capped, and a partner reused
under a different name is flagged for the human. Notes are HTML-escaped before Odoo renders
them, the alert bot's text is escaped too, and the dashboard renders messages as text nodes,
never as markup. The same customer asking again within ten minutes gets the same lead
instead of a duplicate.

**Alerts use a separate bot.** It has its own token, messages only the owner's chat, and
cannot be reached by customers. A failure to send an alert, to mirror to Supabase, or to
create a helper record in Odoo never costs the customer their reply or the lead.

**What the assistant can change, and what stops it doing more.** Beyond searching, the
assistant can create a lead, hold an item, and request a viewing. Each is limited in code
rather than by the prompt. One state-changing action is allowed per message, and a chat gets
three an hour. A hold or a viewing is only accepted for an item that a search in that chat has already shown
the customer. An item can only be held if it exists in the catalog for this domain and is
available, and Odoo decides that inside one transaction under a lock, so two customers
cannot get the same item. One customer (identified by email or phone) can hold one item, and
ten holds can be active at once. A hold lapses after 24 hours and a scheduled job puts the
item back. The price on every one of these comes from the catalog. A viewing date must be
between today and sixty days ahead. No tool can change an existing lead or mark anything
sold. Only the owner can, through buttons the customer cannot reach.

**Contact details must come from the customer.** A model can be talked into (or fed, through
a poisoned catalog entry) an email or number that no customer gave, which would send
follow-ups to an attacker. A contact detail is recorded only if it appears in the customer's
own messages. Anything the model adds on its own is dropped, and anything the customer wrote
that the model left out is added back. The match is strict. An email must be the whole address,
not the end of a longer one. A phone number must be the digits the customer typed, or the same
digits with the shop's country code in front (`DEFAULT_PHONE_COUNTRY_CODE`, 971 unless you
change it). A different prefix or a shortened tail is a different subscriber. The customer's
messages are joined with a separator, so digits at the end of one message and the start of the
next never read as one number.

**Catalog text is treated as untrusted.** A catalog entry is text someone typed, and it goes
to the model as tool output. Before the model reads it, links, chat-template tokens and known
injection phrases are removed and long fields are cut. The raw result is kept for the owner's
alert, and the alert is escaped, so the cleaning affects only what the model sees.

**The owner channel.** The alert bot has its own webhook, which refuses everything unless a
secret is configured and compares it in constant time (on the bytes, so a hostile header
is refused and cannot crash the route). On top of that, every update is
ignored unless it comes from the owner's chat and the owner's account, so someone who finds
the address and the secret still cannot act as the owner without controlling that account.
Nothing a customer sends is routed to it. Customer messages that look like owner commands
(`/open`, button data) are ordinary text to the assistant, and the red-team set includes them.
While a chat is in human mode the customer's text is forwarded to the owner escaped.

**Talk mode and phone numbers.** Talk mode is only reachable from the owner's chat, like every
other owner control, and a customer typing `/talk` is an ordinary customer. A customer's Telegram
username becomes a link only if it matches Telegram's own pattern (5 to 32 letters, digits or
underscores, starting with a letter), so a hostile name cannot turn "Open chat" into another
address. A shared contact is used only if Telegram says it is the sender's own (the contact's
user id equals the sender's), because anyone can forward someone else's contact card. A number the
customer types is used only after the bot asked for one, only if it has at least nine digits and is
written like a phone number (a plus, a leading zero or the country code), so a price such as
"1 200 000" or a date is not taken for a phone. Numbers are asked for and accepted only in a
private chat, never in a group where anyone could answer. Numbers are checked for length and shape and
written in international form. Text the customer sends while in human mode reaches the owner
escaped and cut to a length Telegram accepts.

**Prompt injection is contained, not solved.** No prompt can make a model immune, and adaptive
attacks are known to get past prompt-only defences, so the limits above sit outside the
model. The red-team set (`eval/datasets/redteam_set.json`) covers the new abilities: holding
every car, chaining actions in one message, forged item ids, impossible dates, markup in
notes, a poisoned catalog, owner commands typed by a customer, and attempts to list the tool
definitions. The results are in the README. What remains possible is described next.

## What a formal security review would still flag

**A determined person can still tie up inventory.** Holds are limited per customer and
overall, but a customer identity is an email or phone number that anyone can invent. Someone
with many Telegram accounts could keep up to ten items on hold, renewing after each 24 hour
lapse. The owner sees every hold and can release it, and lowering the
`leadgate.max_active_holds` parameter shrinks the exposure. A real deployment would want
verified contact details before a hold.

**Run one worker.** Conversation history, update de-duplication, the rate limits and the
reminder task live in the engine's memory or start once per process. With two workers the
limits would not be shared and each worker would send its own reminders. The reminder claim
(a tag in Odoo) stops repeats after a restart, not two processes acting at the same moment.
Hold limits are different: they are enforced inside Odoo and hold across any number of workers.

**Owner controls need the right chat id.** Buttons and replies work only from the owner's
private chat with the alert bot, so `TELEGRAM_ALERTS_CHAT_ID` must be that chat's positive
number. A group or a username would leave the alerts arriving and the buttons ignored; the
engine logs an error at startup if the value cannot work.

**A username is not an identity.** Telegram usernames can be changed and given up, so "Open chat"
opens whoever holds that name now, which may not be the person who wrote to the bot. The bot's own
chat with the customer is the reliable channel, and the phone number the customer shared is the
other. Numbers are personal data: they are stored in Odoo, on the dashboard's copy of the lead
(staff only), and in the store's chat record, which expires after 180 days.

**A hostile customer can still waste the owner's time.** Three leads an hour per chat is a
limit per chat, and chats are free to create. Every lead is alerted, tagged and assigned.
The alert bot cannot be muted per customer. Rate limiting by Telegram account and a block
list are not built.

**The assistant's words are only filtered, not verified.** The reply filter removes links and
prompt leaks, and tool results carry notes telling the model not to promise a hold or a time.
A model can still say something inaccurate about what will happen next, and a reader would
have to catch it.

**Human mode forgets what the owner said.** The owner's messages are logged on the lead in
Odoo, but they are not part of the history the assistant reads after the chat is handed back.

**The store keeps chat ids for 180 days.** The record of which chat a lead came from (needed
to answer the customer) expires on its own after `LEAD_CHAT_RETENTION_DAYS`, and a handoff
expires when its time is up. Conversation text in MongoDB and Supabase has no such limit.

**No data-residency guarantee from the free-tier LLM provider.** Every customer message
is sent to OpenRouter's free tier (a rotating list of free models, set in
`engine/config.py` or `OPENROUTER_MODELS`), with Mistral as a fallback. Neither the free-tier terms nor this project's configuration make any
data-residency, retention, or no-training guarantee. A real deployment handling actual
customer conversations would need a paid tier with an explicit data-processing agreement
before this is acceptable.

**Sign-ups stay open unless you switch them off.** The staff-only policy protects the data
either way, but there is no reason for strangers to be able to register. Turn off "Allow new
users to sign up" in the Supabase dashboard. This project cannot do it for you without an
access token it does not hold.

**Conversation text is stored in plaintext with no retention limit.** Messages sit in
MongoDB and in Supabase for as long as you keep them. A real deployment would decide how long
to keep customer messages and delete on request.

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

**Prompt-injection defenses are layered, not proven.** The controls under "What's
covered" make the worst outcomes structurally impossible whatever the model says: a
bad argument never reaches Odoo, a lead's price is only kept if the catalog has it, and
leads are capped per chat. They do not stop the model from being steered into a valid
but unwanted action, such as a search with odd filters or a lead carrying false customer
details, and the phrase screen in front of the model is easy to word around. The
red-team set is 26 hand-written attacks against one free-tier model, so a pass rate
there says little about other models or attacks it does not contain.

**Negation-blind free-text matching in the eval harness.** Not a production security
issue, but worth naming here since it was found during an adversarial review of this
project's own metrics: `eval/metrics.py`'s free-text matcher checks for word overlap
without checking for negation, so in principle a reply containing "not a cash buyer"
could score as matching an expected value of "cash buyer." No real case in the current
93-case suite triggers this, but it's a real, unfixed gap in the harness itself. See
`eval/REPORT.md` for the full account.
