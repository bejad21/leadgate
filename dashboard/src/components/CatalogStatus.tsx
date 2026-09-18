import { useMemo } from 'react'
import type { DomainType } from '../lib/useCatalogItems'
import { useCatalogItems } from '../lib/useCatalogItems'

interface CatalogStatusProps {
  domain: DomainType
}

const STATUS_ORDER = ['available', 'reserved', 'sold'] as const

const TILE_STYLES: Record<(typeof STATUS_ORDER)[number], string> = {
  available: 'border-emerald-300 dark:border-emerald-800',
  reserved: 'border-amber-300 dark:border-amber-800',
  sold: 'border-slate-300 dark:border-slate-700',
}

const TILE_LABEL_STYLES: Record<(typeof STATUS_ORDER)[number], string> = {
  available: 'text-emerald-700 dark:text-emerald-400',
  reserved: 'text-amber-700 dark:text-amber-400',
  sold: 'text-slate-600 dark:text-slate-400',
}

/**
 * Summary panel: counts of available/reserved/sold for the selected domain.
 * Single numbers per the dataviz skill -- stat tiles, no chart needed.
 * Shares the same data source as LiveLeadsFeed via useCatalogItems so the
 * Realtime subscription is only wired up in one place.
 */
export function CatalogStatus({ domain }: CatalogStatusProps) {
  const { items, loading } = useCatalogItems()

  const counts = useMemo(() => {
    const base: Record<string, number> = { available: 0, reserved: 0, sold: 0 }
    for (const item of items) {
      if (item.domain_type !== domain) continue
      const key = (item.status ?? '').toLowerCase()
      if (key in base) {
        base[key] += 1
      }
    }
    return base
  }, [items, domain])

  return (
    <section aria-labelledby="catalog-status-heading" className="text-left">
      <h2 id="catalog-status-heading" className="text-lg font-semibold text-[var(--text-h)]">
        Catalog Status
      </h2>
      <div className="mt-3 grid grid-cols-3 gap-3">
        {STATUS_ORDER.map((status) => (
          <div
            key={status}
            className={`rounded-lg border-2 px-4 py-3 text-center ${TILE_STYLES[status]}`}
          >
            <p className="text-2xl font-semibold tabular-nums text-[var(--text-h)]">
              {loading ? '—' : counts[status]}
            </p>
            <p className={`mt-1 text-xs font-medium uppercase tracking-wide ${TILE_LABEL_STYLES[status]}`}>
              {status}
            </p>
          </div>
        ))}
      </div>
    </section>
  )
}
