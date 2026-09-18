import { useEffect, useState } from 'react'
import type { RealtimePostgresChangesPayload } from '@supabase/supabase-js'
import { supabase } from './supabaseClient'

export type DomainType = 'cars' | 'real_estate'
export type CatalogStatus = 'available' | 'reserved' | 'sold'

export interface CatalogItem {
  id: number
  odoo_id: number
  domain_type: DomainType
  name: string | null
  price: number | null
  status: CatalogStatus | string | null
  updated_at: string
}

/**
 * Shared data hook for `catalog_items`: does an initial full fetch (the
 * Realtime channel only delivers *future* changes, not existing rows) and
 * then keeps the in-memory list in sync via a Postgres Changes subscription.
 *
 * Both LiveLeadsFeed and CatalogStatus consume this hook so the subscription
 * logic lives in exactly one place.
 */
export function useCatalogItems() {
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

    const channel = supabase
      .channel('catalog_items-changes')
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

  return { items, loading, error }
}
