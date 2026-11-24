import type { ToolCallRecord } from './privateTypes'

export interface ToolNote {
  title: string
  details: string[]
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
const plain = new Intl.NumberFormat('en-US')

const SORT_WORDS: Record<string, string> = {
  price_asc: 'cheapest first',
  price_desc: 'dearest first',
  mileage_asc: 'lowest mileage first',
  year_desc: 'newest first',
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** The order details are shown in, and how each argument reads. */
const SEARCH_DETAILS: [string, (value: unknown) => string | null][] = [
  ['make', text],
  ['model', text],
  ['property_type', text],
  ['condition', text],
  ['bedrooms', (v) => (num(v) == null ? null : `${num(v)} bed`)],
  ['year_min', (v) => (num(v) == null ? null : `${num(v)} or newer`)],
  ['mileage_max', (v) => (num(v) == null ? null : `under ${plain.format(num(v)!)} mi`)],
  ['location', (v) => (text(v) ? `in ${text(v)}` : null)],
  ['price_min', (v) => (num(v) == null ? null : `from ${usd.format(num(v)!)}`)],
  ['price_max', (v) => (num(v) == null ? null : `up to ${usd.format(num(v)!)}`)],
  ['sort_by', (v) => SORT_WORDS[String(v)] ?? null],
]

/** The item's number, which is the number on its key. */
function itemNumber(value: unknown): string | null {
  const id = num(value)
  return id == null ? null : `No. ${id}`
}

/** "2026-09-26" as "Sat 26 Sep". Noon avoids the day shifting with the time zone. */
function shortDay(value: unknown): string | null {
  const raw = text(value)
  if (!raw || !/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null
  const date = new Date(`${raw}T12:00:00`)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric', month: 'short' })
}

/** What the assistant did, in words a person would use. */
export function describeToolCall(call: ToolCallRecord): ToolNote {
  const args = call.arguments ?? {}

  if (call.name === 'search_inventory' || call.name === 'search_listings') {
    const details: string[] = []
    for (const [key, format] of SEARCH_DETAILS) {
      if (key in args) {
        const shown = format(args[key])
        if (shown) details.push(shown)
      }
    }
    return { title: call.name === 'search_inventory' ? 'Searched the cars' : 'Searched the homes', details }
  }

  if (call.name === 'create_lead') {
    const details: string[] = []
    const item = text(args.name)
    if (item) details.push(item)
    const price = num(args.price)
    if (price != null) details.push(usd.format(price))
    return { title: 'Created a lead', details }
  }

  if (call.name === 'reserve_item') {
    const number = itemNumber(args.item_id)
    return { title: 'Held an item', details: number ? [number] : [] }
  }

  if (call.name === 'book_viewing') {
    const details: string[] = []
    const number = itemNumber(args.item_id)
    if (number) details.push(number)
    const day = shortDay(args.date)
    if (day) details.push(day)
    const slot = text(args.slot)
    if (slot) details.push(slot)
    return { title: 'Requested a viewing', details }
  }

  return { title: call.name ?? 'Used a tool', details: [] }
}
