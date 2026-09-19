import type { CatalogItem } from '../lib/useCatalogItems'
import { formatPrice, formatWhen, normalizeStatus, STATUS_LABEL } from '../lib/format'

interface TagReaderProps {
  item: CatalogItem | null
  pinned: boolean
}

/** A brass nameplate that reads out whichever key is pointed at or picked. */
export function TagReader({ item, pinned }: TagReaderProps) {
  if (!item) {
    return (
      <section className="reader reader-idle" aria-label="Key details">
        <p>Point at or tap a key to read its tag.</p>
      </section>
    )
  }

  const status = normalizeStatus(item.status)
  return (
    <section className="reader" aria-label="Key details" aria-live="polite" data-status={status}>
      <div className="reader-number">
        <span className="reader-hash" aria-hidden="true">No.</span>
        {item.odoo_id}
      </div>
      <p className="reader-name">{item.name ?? 'Unnamed item'}</p>
      <dl className="reader-facts">
        <div>
          <dt>Price</dt>
          <dd>{formatPrice(item.price)}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{STATUS_LABEL[status]}</dd>
        </div>
        <div>
          <dt>Last change</dt>
          <dd>{formatWhen(item.updated_at) || 'Unknown'}</dd>
        </div>
      </dl>
      {pinned && <p className="reader-pin">Picked. Select it again to let go.</p>}
    </section>
  )
}
