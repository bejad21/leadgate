import { useState } from 'react'
import type { FormEvent } from 'react'

interface SignInPlateProps {
  onSignIn: (email: string, password: string) => Promise<string | null>
}

/** A brass plate with the staff sign-in. Real leads and messages sit behind it. */
export function SignInPlate({ onSignIn }: SignInPlateProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const failure = await onSignIn(email.trim(), password)
    setBusy(false)
    if (failure) setError(failure)
  }

  return (
    <form className="signin" onSubmit={submit} aria-labelledby="signin-title">
      <h2 id="signin-title" className="signin-title">
        Staff sign-in
      </h2>
      <p className="signin-copy">These are sample conversations. Sign in to see your own leads and what customers said.</p>
      <div className="signin-fields">
        <label className="signin-field">
          <span>Email</span>
          <input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} />
        </label>
        <label className="signin-field">
          <span>Password</span>
          <input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <button type="submit" className="signin-button" disabled={busy}>
          {busy ? 'Signing in' : 'Sign in'}
        </button>
      </div>
      {error && (
        <p className="signin-error" role="alert">
          {error}
        </p>
      )}
    </form>
  )
}
