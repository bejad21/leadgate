import type { CatalogChange, CatalogItem } from './useCatalogItems'
import { normalizeStatus } from './format'

export interface SheetRow {
  key: string
  item: CatalogItem
  to: string | null
  at: string
  live: boolean
}

/** Live changes first, then whatever was already on hold or sold. */
export function buildSheetRows(items: CatalogItem[], changes: CatalogChange[], limit: number): SheetRow[] {
  const seen = new Set<number>()
  const rows: SheetRow[] = []

  for (const change of changes) {
    if (seen.has(change.item.id)) continue
    seen.add(change.item.id)
    rows.push({ key: change.key, item: change.item, to: change.to, at: change.at, live: true })
  }

  const earlier = items
    .filter((item) => normalizeStatus(item.status) !== 'available' && !seen.has(item.id))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  for (const item of earlier) {
    rows.push({ key: `earlier-${item.id}`, item, to: item.status, at: item.updated_at, live: false })
  }

  return rows.slice(0, limit)
}

