import type { DomainType } from './useCatalogItems'
import type { LeadKind, LeadStatus } from './format'

/** A lead the assistant created, mirrored from the engine. Private: sign-in only. */
export interface Lead {
  id: number
  odoo_lead_id: number
  domain_type: DomainType
  item_name: string
  customer_name: string | null
  email: string | null
  phone: string | null
  price: number | null
  price_verified: boolean
  /** A plain lead, a hold on an item, or a viewing request. */
  kind: LeadKind
  /** Where the owner has taken it: new until someone acts. */
  status: LeadStatus
  /** "Held for 24 hours", "Sat 26 Sep, afternoon". */
  detail: string | null
  chat_ref: string
  created_at: string
}

export interface ToolCallRecord {
  name: string | null
  arguments: Record<string, unknown> | null
}

/** One customer message and the assistant's reply. Private: sign-in only. */
export interface Turn {
  id: number
  chat_ref: string
  domain_type: DomainType
  user_message: string
  reply: string
  tool_calls: ToolCallRecord[]
  blocked: boolean
  created_at: string
}
