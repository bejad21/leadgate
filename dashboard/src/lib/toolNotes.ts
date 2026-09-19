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

  return { title: call.name ?? 'Used a tool', details: [] }
}
