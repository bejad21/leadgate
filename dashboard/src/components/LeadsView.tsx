import { useMemo, useRef, useState } from 'react'
import { buildConversations } from '../lib/conversations'
import { DOMAIN_LABEL, formatWhen } from '../lib/format'
import type { Lead, Turn } from '../lib/privateTypes'
import { sampleData } from '../lib/sampleData'
import { ConversationRoll } from './ConversationRoll'
import { LeadSlip } from './LeadSlip'
import { SignInPlate } from './SignInPlate'

interface LeadsViewProps {
  signedIn: boolean
  leads: Lead[]
  turns: Turn[]
  loading: boolean
  error: string | null
  onSignIn: (email: string, password: string) => Promise<string | null>
}

const OUTCOME_LABEL = { lead: 'Lead', browsing: 'Browsing', 'turned away': 'Turned away' } as const

/** The pad of message slips, the other conversations, and the roll for whichever is picked. */
export function LeadsView({ signedIn, leads, turns, loading, error, onSignIn }: LeadsViewProps) {
  const sample = useMemo(() => sampleData(), [])
  const shown = signedIn ? { leads, turns } : sample
  const conversations = useMemo(() => buildConversations(shown.leads, shown.turns), [shown.leads, shown.turns])

  const [pickedKey, setPickedKey] = useState<string | null>(null)
  const rollRef = useRef<HTMLDivElement>(null)

  const all = [...conversations.slips, ...conversations.others]
  const active = all.find((conversation) => conversation.key === pickedKey) ?? all[0] ?? null

  function pick(key: string) {
    setPickedKey(key)
    if (window.matchMedia('(max-width: 1040px)').matches) {
      const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      rollRef.current?.scrollIntoView({ behavior: calm ? 'auto' : 'smooth', block: 'start' })
    }
  }

  const { counts } = conversations
  const tallies = [
    { label: 'Conversations', count: counts.conversations },
    { label: 'Leads', count: counts.leads },
    { label: 'Turned away', count: counts.turnedAway },
  ]

  return (
    <>
      <div className="intro">
        <h1 className="headline">Every lead, and what was said.</h1>
        <div className="intro-side">
          <p className="intro-copy">
            {signedIn
              ? 'Live from Telegram. A new lead lands on the pad the moment it is created.'
              : 'Sample conversations, so you can see how it works. Sign in to see your own.'}
          </p>
          <ul className="tapes" aria-label="Totals">
            {tallies.map((tally) => (
              <li key={tally.label} className="tape tape-plain">
                <span className="tape-label">{tally.label}</span>
                <span className="tape-count">{signedIn && loading ? '···' : tally.count}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <main className="board leads-board">
        <div className="pad">
          {!signedIn && <SignInPlate onSignIn={onSignIn} />}
          {error && (
            <p className="pad-error" role="alert">
              Could not read your leads: {error}
            </p>
          )}

          {signedIn && !loading && all.length === 0 ? (
            <p className="pad-empty">
              No conversations yet. When a customer messages the bot, the conversation and any lead it produces will appear here.
            </p>
          ) : (
            <>
              <ul className="slips" aria-label="Leads">
                {conversations.slips.map((conversation) => (
                  <LeadSlip key={conversation.key} conversation={conversation} selected={active?.key === conversation.key} onSelect={pick} />
                ))}
              </ul>

              {conversations.others.length > 0 && (
                <section className="others" aria-label="Conversations without a lead">
                  <h2 className="others-title">No lead</h2>
                  <ul className="others-list">
                    {conversations.others.map((conversation) => (
                      <li key={conversation.key}>
                        <button
                          type="button"
                          className="other"
                          data-selected={active?.key === conversation.key || undefined}
                          aria-pressed={active?.key === conversation.key}
                          onClick={() => pick(conversation.key)}
                        >
                          <span className="other-time">{formatWhen(conversation.lastAt)}</span>
                          <span className="other-text">{conversation.firstMessage}</span>
                          <span className="other-meta">
                            {DOMAIN_LABEL[conversation.domain]}
                            <span className="other-outcome" data-outcome={conversation.outcome}>
                              {OUTCOME_LABEL[conversation.outcome]}
                            </span>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>

        <aside className="roll-desk" ref={rollRef}>
          <ConversationRoll conversation={active} sample={!signedIn} onBack={() => window.scrollTo({ top: 0, behavior: 'auto' })} />
        </aside>
      </main>
    </>
  )
}
