import type { Status } from '../lib/format'
import { STATUS_LABEL } from '../lib/format'
import { CountUp } from './CountUp'

interface TapeCountsProps {
  counts: Record<Status, number>
  loading: boolean
}

const ORDER: Status[] = ['available', 'reserved', 'sold']

/** The status tally. Each strip also shows how its status looks on the wall, so it doubles as the legend. */
export function TapeCounts({ counts, loading }: TapeCountsProps) {
  return (
    <ul className="tapes" aria-label="Keys by status">
      {ORDER.map((status) => (
        <li key={status} className="tape" data-status={status}>
          <span className="tape-glyph" aria-hidden="true" />
          <span className="tape-label">{STATUS_LABEL[status]}</span>
          <span className="tape-count">{loading ? '···' : <CountUp value={counts[status]} />}</span>
        </li>
      ))}
    </ul>
  )
}
