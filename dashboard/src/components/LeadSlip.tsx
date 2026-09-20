import type { Conversation } from '../lib/conversations'
import { DOMAIN_LABEL, LEAD_KIND_LABEL, LEAD_STATUS_LABEL, formatPrice, formatWhen, normalizeLeadKind, normalizeLeadStatus } from '../lib/format'

interface LeadSlipProps {
  conversation: Conversation
  selected: boolean
  onSelect: (key: string) => void
}

/** A lead as a message slip pinned to the pad. */
export function LeadSlip({ conversation, selected, onSelect }: LeadSlipProps) {
  const lead = conversation.lead!
  const contact = [lead.email, lead.phone].filter(Boolean)
  const kind = normalizeLeadKind(lead.kind)
  const status = normalizeLeadStatus(lead.status)
  const label = `${lead.customer_name ?? 'Unnamed customer'}, ${lead.item_name}, ${formatPrice(lead.price)}`
  const spoken = [kind !== 'lead' ? LEAD_KIND_LABEL[kind] : null, lead.detail, status !== 'new' ? LEAD_STATUS_LABEL[status] : null].filter(Boolean).join(', ')

  return (
    <li>
      <button
        type="button"
        className="slip"
        data-selected={selected || undefined}
        data-domain={lead.domain_type}
        aria-pressed={selected}
        aria-label={`${kind === 'lead' ? 'Lead' : LEAD_KIND_LABEL[kind]}: ${label}${spoken ? `, ${spoken}` : ''}`}
        onClick={() => onSelect(conversation.key)}
        style={{ ['--tilt' as string]: `${((Math.abs(lead.odoo_lead_id) % 5) - 2) * 0.35}deg` }}
      >
        <span className="slip-pin" aria-hidden="true" />
        <span className="slip-head">
          <span className="slip-time">{formatWhen(lead.created_at)}</span>
          <span className="slip-kind">{DOMAIN_LABEL[lead.domain_type]}</span>
        </span>
        <span className="slip-item">{lead.item_name}</span>
        {(kind !== 'lead' || lead.detail) && (
          <span className="slip-tag" data-kind={kind}>
            {kind !== 'lead' && <span className="slip-tag-kind">{LEAD_KIND_LABEL[kind]}</span>}
            {lead.detail && <span className="slip-tag-detail">{lead.detail}</span>}
          </span>
        )}
        <span className="slip-price">
          {formatPrice(lead.price)}
          {!lead.price_verified && <span className="slip-flag">Price not verified</span>}
        </span>
        <span className="slip-who">{lead.customer_name ?? 'No name given'}</span>
        <span className="slip-contact">{contact.length ? contact.join('  ') : 'No contact given'}</span>
        {status !== 'new' && (
          <span className="slip-status" data-status={status}>
            {LEAD_STATUS_LABEL[status]}
          </span>
        )}
      </button>
    </li>
  )
}
