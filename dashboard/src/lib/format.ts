import type { DomainType } from './useCatalogItems'

export type Status = 'available' | 'reserved' | 'sold'

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

export function formatPrice(price: number | null): string {
  return price == null ? 'Price on request' : usd.format(price)
}

/** Time for today's changes, date for older ones. */
export function formatWhen(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  if (date.toDateString() === new Date().toDateString()) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/** Anything unrecognised is treated as on the board. */
export function normalizeStatus(status: string | null): Status {
  return status === 'reserved' || status === 'sold' ? status : 'available'
}

export const STATUS_LABEL: Record<Status, string> = {
  available: 'On the board',
  reserved: 'On hold',
  sold: 'Sold',
}

/** What a sign-out sheet stamp says when an item moves *to* a status. */
export const STAMP_LABEL: Record<Status, string> = {
  available: 'Back on board',
  reserved: 'On hold',
  sold: 'Sold',
}

export const DOMAIN_LABEL: Record<DomainType, string> = {
  cars: 'Cars',
  real_estate: 'Homes',
}

export type LeadKind = 'lead' | 'reservation' | 'viewing'
export type LeadStatus = 'new' | 'taken' | 'contacted' | 'confirmed' | 'won' | 'lost' | 'released'

export const LEAD_KIND_LABEL: Record<LeadKind, string> = {
  lead: 'Lead',
  reservation: 'Hold',
  viewing: 'Viewing',
}

export const LEAD_STATUS_LABEL: Record<LeadStatus, string> = {
  new: 'New',
  taken: 'Taken',
  contacted: 'Contacted',
  confirmed: 'Confirmed',
  won: 'Won',
  lost: 'Lost',
  released: 'Released',
}

/** Anything unrecognised, or missing, is an ordinary lead. */
export function normalizeLeadKind(kind: string | null | undefined): LeadKind {
  return kind === 'reservation' || kind === 'viewing' ? kind : 'lead'
}

/** Anything unrecognised, or missing, has not been acted on yet. */
export function normalizeLeadStatus(status: string | null | undefined): LeadStatus {
  return status && status in LEAD_STATUS_LABEL ? (status as LeadStatus) : 'new'
}
