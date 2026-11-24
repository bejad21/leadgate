import { useCallback, useEffect, useState } from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from './supabaseClient'

/**
 * Who is signed in. Leads and conversations are private, so the Leads view asks
 * for a Supabase Auth session. `ready` turns true once the stored session (if
 * any) has been read, so the page never flashes the signed-out view at someone
 * who is signed in.
 */
export function useSession() {
  const [session, setSession] = useState<Session | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let active = true
    supabase.auth.getSession().then(({ data }) => {
      if (!active) return
      setSession(data.session)
      setReady(true)
    })
    const { data } = supabase.auth.onAuthStateChange((_event, next) => {
      if (active) setSession(next)
    })
    return () => {
      active = false
      data.subscription.unsubscribe()
    }
  }, [])

  /** Returns an error message, or null when it worked. */
  const signIn = useCallback(async (email: string, password: string): Promise<string | null> => {
    const { error } = await supabase.auth.signInWithPassword({ email, password })
    if (!error) return null
    return error.status === 400 ? 'That email and password do not match.' : error.message
  }, [])

  const signOut = useCallback(async () => {
    await supabase.auth.signOut()
  }, [])

  return { session, ready, signIn, signOut }
}
