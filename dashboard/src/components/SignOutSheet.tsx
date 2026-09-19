import { formatPrice, formatWhen, normalizeStatus, STAMP_LABEL } from '../lib/format'
import type { SheetRow } from '../lib/sheetRows'

interface SignOutSheetProps {
  rows: SheetRow[]
  loading: boolean
}

/** Paper sign-out sheet on a steel clip. Each row is stamped in ink. */
export function SignOutSheet({ rows, loading }: SignOutSheetProps) {
  return (
    <section className="sheet" aria-label="Recent changes" aria-live="polite">
      <div className="sheet-clip" aria-hidden="true" />
      <h2 className="sheet-title">Sign-out sheet</h2>
      {rows.length === 0 ? (
        <p className="sheet-empty">
          {loading
            ? 'Reading the sheet…'
            : 'Nothing has moved yet. Change a status in Odoo and it lands here, stamped.'}
        </p>
      ) : (
        <ol className="sheet-rows">
          {rows.map((row) => {
            const status = normalizeStatus(row.to)
            return (
              <li key={row.key} className="sheet-row" data-live={row.live || undefined}>
                <span className="sheet-time">{formatWhen(row.at)}</span>
                <span className="sheet-what">
                  <span className="sheet-name">{row.item.name ?? 'Unnamed item'}</span>
                  <span className="sheet-price">
                    No. {row.item.odoo_id} · {formatPrice(row.item.price)}
                  </span>
                </span>
                <span className="stamp" data-status={status} style={{ ['--tilt' as string]: `${(row.item.id % 5) - 2.5}deg` }}>
                  {STAMP_LABEL[status]}
                </span>
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
