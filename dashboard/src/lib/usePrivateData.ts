import { useEffect, useState } from 'react'
import type { RealtimePostgresChangesPayload } from '@supabase/supabase-js'
import { mergeRows } from './mergeRows'
import { supabase } from './supabaseClient'
import type { Lead, Turn } from './privateTypes'

const LEAD_LIMIT = 200
// Enough for a demo-sized deployment. A busy one would page or aggregate instead.
const TURN_LIMIT = 1000

interface PrivateState {
  /** Whose data this is. A different signed-in user starts from nothing. */
  forUser: string | null
  leads: Lead[]
  turns: Turn[]
  loading: boolean
  error: string | null
}

const EMPTY: PrivateState = { forUser: null, leads: [], turns: [], loading: false, error: null }
const LOADING: PrivateState = { ...EMPTY, loading: true }

function forUser(current: PrivateState, userId: string): PrivateState {
  return current.forUser === userId ? current : { ...LOADING, forUser: userId }
}

/**
 * Leads and the conversation turns behind them: an initial read plus live
 * changes. Nothing is requested unless someone is signed in (`userId`). The
 * database only answers staff accounts (row level security), and the state is
 * tagged with whose it is, so signing out and in as someone else never shows the
 * previous person's rows, even for a moment.
 */
export function usePrivateData(userId: string | null): PrivateState {
  const [state, setState] = useState<PrivateState>(LOADING)

  useEffect(() => {
    if (!userId) return
    let active = true

    async function load(id: string) {
      const [leads, turns] = await Promise.all([
        supabase.from('leads').select('*').order('created_at', { ascending: false }).limit(LEAD_LIMIT),
        supabase.from('conversation_turns').select('*').order('created_at', { ascending: false }).limit(TURN_LIMIT),
      ])
      if (!active) return
      const failure = leads.error ?? turns.error
      setState((current) => {
        const base = forUser(current, id)
        return {
          forUser: id,
          // rows that arrived live before this read finished are kept
          leads: mergeRows(base.leads, (leads.data ?? []) as Lead[]),
          turns: mergeRows(base.turns, (turns.data ?? []) as Turn[]),
          loading: false,
          error: failure ? failure.message : null,
        }
      })
    }
    load(userId)

    // A unique channel name per subscription: supabase-js reuses channels by name.
    const channel = supabase
      .channel(`private-changes-${crypto.randomUUID()}`)
      .on('postgres_changes', { event: '*', schema: 'public', table: 'leads' }, (payload: RealtimePostgresChangesPayload<Lead>) => {
        if (payload.eventType === 'DELETE') return
        const row = payload.new as Lead
        setState((current) => {
          const base = forUser(current, userId)
          return { ...base, leads: [row, ...base.leads.filter((lead) => lead.id !== row.id)] }
        })
      })
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'conversation_turns' }, (payload: RealtimePostgresChangesPayload<Turn>) => {
        const row = payload.new as Turn
        setState((current) => {
          const base = forUser(current, userId)
          return { ...base, turns: [row, ...base.turns.filter((turn) => turn.id !== row.id)] }
        })
      })
      .subscribe()

    return () => {
      active = false
      supabase.removeChannel(channel)
    }
  }, [userId])

  if (!userId) return EMPTY
  return state.forUser === userId ? state : LOADING
}
