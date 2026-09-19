import { memo, useEffect, useRef, useState } from 'react'
import type { CatalogItem } from '../lib/useCatalogItems'
import { formatPrice, normalizeStatus, STATUS_LABEL } from '../lib/format'

interface KeyTagProps {
  item: CatalogItem
  /** Delay for the one-time drop-in wave, in ms. */
  dropDelay: number
  selected: boolean
  focusable: boolean
  onHover: (item: CatalogItem | null) => void
  onSelect: (item: CatalogItem) => void
}

/**
 * One catalog item as a key on a hook.
 *  - available: the tag hangs straight
 *  - reserved: the tag is flipped up on its hook
 *  - sold: the key is gone and only the empty hook is left
 * Status is carried by pose and presence as well as colour.
 */
function KeyTagBase({ item, dropDelay, selected, focusable, onHover, onSelect }: KeyTagProps) {
  const status = normalizeStatus(item.status)
  const previous = useRef(status)
  const [swinging, setSwinging] = useState(false)

  // Swing on the hook when the status changes live. Not on first render.
  useEffect(() => {
    if (previous.current === status) return
    previous.current = status
    setSwinging(true)
    const timer = window.setTimeout(() => setSwinging(false), 1300)
    return () => window.clearTimeout(timer)
  }, [status])

  const label = `Key ${item.odoo_id}, ${item.name ?? 'unnamed item'}, ${formatPrice(item.price)}, ${STATUS_LABEL[status]}`

  return (
    <button
      type="button"
      className="key-cell"
      data-id={item.id}
      data-status={status}
      data-selected={selected || undefined}
      data-swinging={swinging || undefined}
      tabIndex={focusable ? 0 : -1}
      aria-label={label}
      aria-pressed={selected}
      onMouseEnter={() => onHover(item)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(item)}
      onBlur={() => onHover(null)}
      onClick={() => onSelect(item)}
      style={{ ['--drop-delay' as string]: `${dropDelay}ms` }}
    >
      <span className="key-hook" aria-hidden="true" />
      <span className="key-ghost" aria-hidden="true" />
      <span className="key-swing" aria-hidden="true">
        <span className="key-tag">
          <span className="key-number">{item.odoo_id}</span>
        </span>
      </span>
    </button>
  )
}

export const KeyTag = memo(KeyTagBase)
