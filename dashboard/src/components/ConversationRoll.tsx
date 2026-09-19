import type { Conversation } from '../lib/conversations'
import { DOMAIN_LABEL, formatWhen } from '../lib/format'
import { splitBold } from '../lib/richText'
import { describeToolCall } from '../lib/toolNotes'

interface ConversationRollProps {
  conversation: Conversation | null
  sample: boolean
  onBack: () => void
}

function Bold({ text }: { text: string }) {
  return (
    <>
      {splitBold(text).map((run, index) => (run.bold ? <strong key={index}>{run.text}</strong> : <span key={index}>{run.text}</span>))}
    </>
  )
}

/** One conversation, printed on a paper roll, with what the assistant did between the lines. */
export function ConversationRoll({ conversation, sample, onBack }: ConversationRollProps) {
  if (!conversation) {
    return (
      <section className="roll roll-idle" aria-label="Conversation">
        <p>Pick a slip to read the conversation behind it.</p>
      </section>
    )
  }

  const lead = conversation.lead
  const heading = lead ? `${lead.customer_name ?? 'Unnamed customer'}, ${lead.item_name}` : conversation.firstMessage || 'Conversation'

  return (
    <section className="roll" aria-label="Conversation">
      <button type="button" className="roll-back" onClick={onBack}>
        Back to the pad
      </button>
      <header className="roll-head">
        <h2 className="roll-title">{heading}</h2>
        <p className="roll-meta">
          {DOMAIN_LABEL[conversation.domain]}
          {conversation.turns.length > 0 && <> · {formatWhen(conversation.turns[0].created_at)}</>}
          {sample && <span className="roll-sample">Sample</span>}
        </p>
      </header>

      {conversation.turns.length === 0 ? (
        <p className="roll-empty">No messages were stored for this lead.</p>
      ) : (
        <ol className="roll-turns">
          {conversation.turns.map((turn) => (
            <li key={turn.id} className="roll-turn" data-blocked={turn.blocked || undefined}>
              <p className="roll-who">
                Customer <time dateTime={turn.created_at}>{formatWhen(turn.created_at)}</time>
                {turn.blocked && <span className="roll-blocked">Turned away</span>}
              </p>
              <p className="roll-text roll-text-customer">{turn.user_message}</p>

              {turn.tool_calls.map((call, index) => {
                const note = describeToolCall(call)
                return (
                  <p key={index} className="roll-note">
                    <span className="roll-note-title">{note.title}</span>
                    {note.details.length > 0 && <span className="roll-note-details">{note.details.join(' · ')}</span>}
                  </p>
                )
              })}

              <p className="roll-who roll-who-agent">Assistant</p>
              <p className="roll-text">
                <Bold text={turn.reply} />
              </p>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
