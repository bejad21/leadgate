import { useEffect, useMemo, useRef, useState } from 'react'
import type { RealtimePostgresChangesPayload } from '@supabase/supabase-js'
import { supabase } from './supabaseClient'

export type DomainType = 'cars' | 'real_estate'
export type CatalogItemStatus = 'available' | 'reserved' | 'sold'

export interface CatalogItem {
  id: number
  odoo_id: number
  domain_type: DomainType
  name: string | null
  price: number | null
  status: CatalogItemStatus | string | null
  updated_at: string
}

/** One status change seen live, with what it changed from. */
export interface CatalogChange {
  key: string
  item: CatalogItem
  from: string | null
  to: string | null
  at: string
}

const MAX_CHANGES = 30

/**
 * Shared data hook for `catalog_items`: an initial full fetch (the Realtime channel only delivers
 * *future* changes), then a Postgres Changes subscription keeps the list in sync. It also reports
 * whether the channel is connected and records each live status change with the previous status.
 *
 * The page calls this once and filters by domain itself. Pass `domain` to get an already-filtered
 * `items` list.
 */
export function useCatalogItems(domain?: DomainType) {
  const [items, setItems] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [changes, setChanges] = useState<CatalogChange[]>([])

  // The latest list, readable from the Realtime callback without a stale
  // closure. Updated together with the state below.
  const itemsRef = useRef<CatalogItem[]>([])

  useEffect(() => {
    let isMounted = true

    function commit(next: CatalogItem[]) {
      itemsRef.current = next
      setItems(next)
    }

    function record(item: CatalogItem, from: string | null) {
      const change: CatalogChange = {
        key: `${item.id}-${item.updated_at}-${Math.random().toString(36).slice(2, 7)}`,
        item,
        from,
        to: item.status,
        at: item.updated_at,
      }
      setChanges((current) => [change, ...current].slice(0, MAX_CHANGES))
    }

    async function fetchInitial() {
      const { data, error: fetchError } = await supabase
        .from('catalog_items')
        .select('*')

      if (!isMounted) return

      if (fetchError) {
        setError(fetchError.message)
        commit([])
      } else {
        commit((data ?? []) as CatalogItem[])
      }
      setLoading(false)
    }

    fetchInitial()

    // Each hook instance needs its own channel name: supabase-js dedupes channels by topic, so a fixed
    // name makes a second subscriber (or React's dev double mount) get an already-subscribed channel,
    // and calling `.on()` on it throws.
    const channel = supabase
      .channel(`catalog_items-changes-${crypto.randomUUID()}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'catalog_items' },
        (payload: RealtimePostgresChangesPayload<CatalogItem>) => {
          const current = itemsRef.current
          if (payload.eventType === 'INSERT') {
            const row = payload.new as CatalogItem
            commit([...current.filter((item) => item.id !== row.id), row])
            record(row, null)
          } else if (payload.eventType === 'UPDATE') {
            const row = payload.new as CatalogItem
            const previous = current.find((item) => item.id === row.id)
            commit(current.map((item) => (item.id === row.id ? row : item)))
            if (previous && previous.status !== row.status) record(row, previous.status)
          } else if (payload.eventType === 'DELETE') {
            const old = payload.old as Partial<CatalogItem>
            commit(current.filter((item) => item.id !== old.id))
          }
        },
      )
      .subscribe((status) => {
        if (isMounted) setConnected(status === 'SUBSCRIBED')
      })

    return () => {
      isMounted = false
      supabase.removeChannel(channel)
    }
  }, [])

  const filteredItems = useMemo(() => {
    if (!domain) return items
    return items.filter((item) => item.domain_type === domain)
  }, [items, domain])

  return { items: filteredItems, loading, error, connected, changes }
}
