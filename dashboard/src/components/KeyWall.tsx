import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import type { CatalogItem } from '../lib/useCatalogItems'
import { KeyTag } from './KeyTag'

interface KeyWallProps {
  items: CatalogItem[]
  loading: boolean
  error: string | null
  selectedId: number | null
  onHover: (item: CatalogItem | null) => void
  onSelect: (item: CatalogItem) => void
}

/** How many grid columns are laid out right now, read back from CSS. */
function readColumns(grid: HTMLElement | null): number {
  if (!grid) return 1
  const template = getComputedStyle(grid).gridTemplateColumns
  return Math.max(1, template.split(' ').filter(Boolean).length)
}

/**
 * The wall of keys. One hook per catalog item, in stock-number order.
 * The grid is a single tab stop; arrow keys, Home and End move between keys.
 */
export function KeyWall({ items, loading, error, selectedId, onHover, onSelect }: KeyWallProps) {
  const gridRef = useRef<HTMLDivElement>(null)
  const [columns, setColumns] = useState(1)
  const [focusedId, setFocusId] = useState<number | null>(null)
  const [entering, setEntering] = useState(true)

  // Keep the roving tab stop on a key that exists.
  const focusId = items.some((item) => item.id === focusedId) ? focusedId : (items[0]?.id ?? null)

  // The drop-in wave plays once, after the keys first arrive.
  useEffect(() => {
    if (loading) return
    const timer = window.setTimeout(() => setEntering(false), 2600)
    return () => window.clearTimeout(timer)
  }, [loading])

  useLayoutEffect(() => {
    const grid = gridRef.current
    if (!grid) return
    const measure = () => setColumns(readColumns(grid))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(grid)
    return () => observer.disconnect()
  }, [items.length])

  const moveFocus = useCallback(
    (index: number) => {
      const target = items[Math.min(Math.max(index, 0), items.length - 1)]
      if (!target) return
      setFocusId(target.id)
      gridRef.current?.querySelector<HTMLElement>(`[data-id="${target.id}"]`)?.focus()
    },
    [items],
  )

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = items.findIndex((item) => item.id === focusId)
    if (current < 0) return
    const steps: Record<string, number> = {
      ArrowRight: 1,
      ArrowLeft: -1,
      ArrowDown: columns,
      ArrowUp: -columns,
    }
    if (event.key in steps) {
      event.preventDefault()
      moveFocus(current + steps[event.key])
    } else if (event.key === 'Home') {
      event.preventDefault()
      moveFocus(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      moveFocus(items.length - 1)
    }
  }

  if (error) {
    return (
      <div className="wall wall-message" role="alert">
        <p className="wall-message-title">The catalog can't be read.</p>
        <p>
          {error}. Check that VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY are set in
          dashboard/.env.local, and that the catalog_items table exists.
        </p>
      </div>
    )
  }

  if (!loading && items.length === 0) {
    return (
      <div className="wall wall-message">
        <p className="wall-message-title">No keys on this board yet.</p>
        <p>Seed the catalog (data/seed_odoo.py) and the tags will hang themselves up.</p>
      </div>
    )
  }

  return (
    <div className="wall" aria-busy={loading} data-entering={entering || undefined}>
      <div className="wall-rail" aria-hidden="true" />
      <div
        ref={gridRef}
        className="wall-grid"
        role="group"
        aria-label="Key board. Use the arrow keys to move between keys."
        onKeyDown={onKeyDown}
      >
        {loading
          ? Array.from({ length: 96 }, (_, i) => <span key={i} className="key-cell key-cell-empty" aria-hidden="true"><span className="key-hook" /></span>)
          : items.map((item, index) => {
              const column = index % columns
              const row = Math.floor(index / columns)
              return (
                <KeyTag
                  key={item.id}
                  item={item}
                  dropDelay={Math.min(row * 26 + column * 11, 1100)}
                  selected={selectedId === item.id}
                  focusable={focusId === item.id}
                  onHover={onHover}
                  onSelect={onSelect}
                />
              )
            })}
      </div>
    </div>
  )
}
