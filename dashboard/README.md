# LeadGate dashboard

A live board of every catalog item, drawn as a cabinet of keys. Each item is a key on
a hook, and its status is where and how the tag hangs:

| Status | On the board |
|---|---|
| On the board (`available`) | The tag hangs straight, green |
| On hold (`reserved`) | The tag is flipped up on its hook, amber |
| Sold (`sold`) | The key has left the hook. Only an empty hook and a dashed outline remain |

Colour is never the only signal: pose and presence carry the status too.

Change a status in Odoo and the board updates about a second later. The tag swings on its
hook before it settles, the tallies move, and a stamped row lands on the sign-out sheet.

## The Leads tab

A second view shows every lead and the conversation behind it. A lead is a message slip on
a pad. Pick one and the conversation prints on a paper roll: the customer's words in blue
ink, the assistant's in black, and between them a stamped note for each thing the
assistant did ("Searched the cars: Toyota, up to $25,000", "Created a lead", "Held an item:
No. 7"). Chats that never produced a lead are listed below the pad, including attempts the
guardrails turned away.

A hold or a viewing request has its own tag on its slip ("Hold, Held for 24 hours" or
"Viewing, Sat 26 Sep, afternoon"). When the owner acts on a lead from Telegram, a stamp
appears on the slip (Taken, Contacted, Confirmed, Won, Lost or Released) and changes live.
An item's number in a note is the number on its key on the Keys board.

This is private data, so it is behind a sign-in. Anyone not signed in sees clearly labelled
sample conversations bundled with the app, and the app never asks the database for real
ones. New leads arrive live. The database only answers staff accounts (see
`n8n/scripts/setup_supabase_leads.sql`), so a stranger who registers an account still sees
nothing.

## Run it

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check and production build
npm run lint
npm test         # unit tests (Vitest)
python tests/leads_ui_test.py   # browser tests, needs the dev server and .env
```

Create `dashboard/.env.local` with the public Supabase values (never the service key):

```
VITE_SUPABASE_URL=<your SUPABASE_URL>
VITE_SUPABASE_ANON_KEY=<your SUPABASE_ANON_KEY>
```

If the board is empty, backfill the catalog once: `python n8n/scripts/backfill_supabase.py`.

## Using it

- Point at a key, or tap it, to read its tag on the brass plate. Select it again to let go.
- The board is one tab stop. Arrow keys, Home and End move between keys, and Enter picks one.
- Cars and Homes are separate boards. The switch is at the top right.
- On a phone the sign-out sheet comes first, and the brass plate stays at the bottom of the
  screen while you scroll the wall.
- With reduced motion turned on in your system settings, the drop-in and swing animations
  are off.

## How it is built

React 19, TypeScript, Vite and Tailwind CSS v4 (used for its reset; the design is
hand-written CSS in `src/index.css`). Data comes from Supabase with the public anon key,
which Row Level Security limits to read-only.

- `src/lib/useCatalogItems.ts`: initial fetch plus a Realtime subscription. It also reports
  whether the channel is connected and records each live change with what it changed from.
- `src/components/KeyWall.tsx`, `KeyTag.tsx`: the wall, and one key on its hook.
- `src/components/TapeCounts.tsx`, `TagReader.tsx`, `SignOutSheet.tsx`, `DomainSwitch.tsx`.
- Typefaces are self-hosted through Fontsource: Bricolage Grotesque for text and Big
  Shoulders Display for the stamped numerals and label-maker tape.

There is one entrance animation (the keys dropping onto their hooks) and one live one (the
swing). Nothing else moves on its own.
