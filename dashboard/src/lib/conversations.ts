import type { Lead, Turn } from './privateTypes'
import type { DomainType } from './useCatalogItems'

export type Outcome = 'lead' | 'browsing' | 'turned away'

export interface Conversation {
  /** Unique per list entry: a lead's slip is keyed by the lead, anything else by its chat. */
  key: string
  chatRef: string
  domain: DomainType
  turns: Turn[]
  lead: Lead | null
  outcome: Outcome
  firstMessage: string
  lastAt: string
}

export interface Conversations {
  /** One slip per lead, newest first. Two leads from one chat are two slips. */
  slips: Conversation[]
  /** Chats that never produced a lead: browsing, and attempts the guardrails turned away. */
  others: Conversation[]
  counts: { conversations: number; leads: number; turnedAway: number }
}

/** Join leads to the turns of their chats and split the rest out. */
export function buildConversations(leads: Lead[], turns: Turn[]): Conversations {
  const byChat = new Map<string, Turn[]>()
  for (const turn of turns) {
    const list = byChat.get(turn.chat_ref) ?? []
    list.push(turn)
    byChat.set(turn.chat_ref, list)
  }
  for (const list of byChat.values()) list.sort((a, b) => a.created_at.localeCompare(b.created_at))

  function make(chatRef: string, lead: Lead | null): Conversation {
    const list = byChat.get(chatRef) ?? []
    const blocked = list.some((turn) => turn.blocked)
    return {
      key: lead ? `lead-${lead.odoo_lead_id}` : chatRef,
      chatRef,
      domain: lead?.domain_type ?? list[0]?.domain_type ?? 'cars',
      turns: list,
      lead,
      outcome: lead ? 'lead' : blocked ? 'turned away' : 'browsing',
      firstMessage: list[0]?.user_message ?? '',
      lastAt: list.length ? list[list.length - 1].created_at : (lead?.created_at ?? ''),
    }
  }

  const slips = [...leads]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((lead) => make(lead.chat_ref, lead))

  const chatsWithLeads = new Set(leads.map((lead) => lead.chat_ref))
  const others = [...byChat.keys()]
    .filter((chatRef) => !chatsWithLeads.has(chatRef))
    .map((chatRef) => make(chatRef, null))
    .sort((a, b) => b.lastAt.localeCompare(a.lastAt))

  const chats = new Set<string>([...byChat.keys(), ...chatsWithLeads])
  return {
    slips,
    others,
    counts: {
      conversations: chats.size,
      leads: leads.length,
      turnedAway: turns.filter((turn) => turn.blocked).length,
    },
  }
}
