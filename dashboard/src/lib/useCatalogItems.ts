import { useEffect, useMemo, useState } from 'react'
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

/**
 * Shared data hook for `catalog_items`: does an initial full fetch (the
 * Realtime channel only delivers *future* changes, not existing rows) and
 * then keeps the in-memory list in sync via a Postgres Changes subscription.
 *
 * Both LiveLeadsFeed and CatalogStatus consume this hook so the subscription
 * logic lives in exactly one place. Pass `domain` to get back an
 * already-filtered `items` list, so the `item.domain_type === domain`
 * check itself also lives in one place instead of being repeated in every
 * consumer.
 */
export function useCatalogItems(domain?: DomainType) {
  const [items, setItems] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isMounted = true

    async function fetchInitial() {
      const { data, error: fetchError } = await supabase
        .from('catalog_items')
        .select('*')

      if (!isMounted) return

      if (fetchError) {
        // Expected right now: the catalog_items table doesn't exist yet and
        // the anon key is a placeholder. Fail gracefully into an empty list
        // rather than throwing, so the UI still renders.
        setError(fetchError.message)
        setItems([])
      } else {
        setItems((data ?? []) as CatalogItem[])
      }
      setLoading(false)
    }

    fetchInitial()

    // Every hook instance needs its own channel name. supabase-js dedupes
    // channels by topic, so a fixed name makes the second consumer (and
    // React's dev-mode double mount) get back an already-subscribed channel,
    // and calling `.on()` on it throws and blanks the whole page.
    const channel = supabase
      .channel(`catalog_items-changes-${crypto.randomUUID()}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'catalog_items' },
        (payload: RealtimePostgresChangesPayload<CatalogItem>) => {
          setItems((current) => {
            if (payload.eventType === 'INSERT') {
              const newRow = payload.new as CatalogItem
              const withoutDup = current.filter((item) => item.id !== newRow.id)
              return [...withoutDup, newRow]
            }
            if (payload.eventType === 'UPDATE') {
              const updatedRow = payload.new as CatalogItem
              return current.map((item) =>
                item.id === updatedRow.id ? updatedRow : item,
              )
            }
            if (payload.eventType === 'DELETE') {
              const oldRow = payload.old as Partial<CatalogItem>
              return current.filter((item) => item.id !== oldRow.id)
            }
            return current
          })
        },
      )
      .subscribe()

    return () => {
      isMounted = false
      supabase.removeChannel(channel)
    }
  }, [])

  const filteredItems = useMemo(() => {
    if (!domain) return items
    return items.filter((item) => item.domain_type === domain)
  }, [items, domain])

  return { items: filteredItems, loading, error }
}
