import { useMemo } from 'react'
import type { DomainType } from '../lib/useCatalogItems'
import { useCatalogItems } from '../lib/useCatalogItems'
import { StatusBadge } from './StatusBadge'

interface LiveLeadsFeedProps {
  domain: DomainType
}

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

/**
 * Live feed of catalog_items rows for the selected domain, sorted by
 * updated_at desc. Backed by useCatalogItems, which does the initial fetch
 * and keeps the list current via a Realtime `postgres_changes` subscription.
 */
export function LiveLeadsFeed({ domain }: LiveLeadsFeedProps) {
  const { items, loading, error } = useCatalogItems()

  const sortedItems = useMemo(() => {
    return items
      .filter((item) => item.domain_type === domain)
      .slice()
      .sort(
        (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
  }, [items, domain])

  return (
    <section aria-labelledby="live-leads-heading" className="text-left">
      <h2 id="live-leads-heading" className="text-lg font-semibold text-[var(--text-h)]">
        Live Leads
      </h2>

      {loading && (
        <p className="mt-2 text-sm text-[var(--text)]">Loading catalog items...</p>
      )}

      {!loading && error && (
        <p className="mt-2 text-sm text-[var(--text)]">
          Couldn't reach the catalog right now. This is expected until the
          Supabase table and credentials are live.
        </p>
      )}

      {!loading && !error && sortedItems.length === 0 && (
        <p className="mt-2 text-sm text-[var(--text)]">No items yet for this domain.</p>
      )}

      {!loading && sortedItems.length > 0 && (
        <ul className="mt-3 flex flex-col gap-2">
          {sortedItems.map((item) => (
            <li
              key={item.id}
              className="flex items-center justify-between gap-3 rounded-lg border border-[var(--border)] px-3 py-2"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-[var(--text-h)]">
                  {item.name ?? `Item #${item.odoo_id}`}
                </p>
                <p className="text-xs text-[var(--text)]">
                  {item.price != null ? currencyFormatter.format(item.price) : 'No price'}
                  {' · '}
                  Updated {new Date(item.updated_at).toLocaleString()}
                </p>
              </div>
              <StatusBadge status={item.status} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
