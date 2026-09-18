const STATUS_STYLES: Record<string, string> = {
  available: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  reserved: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  sold: 'bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
}

const DOT_STYLES: Record<string, string> = {
  available: 'bg-emerald-500',
  reserved: 'bg-amber-500',
  sold: 'bg-slate-500',
}

const FALLBACK_BADGE = 'bg-slate-100 text-slate-600 dark:bg-slate-900 dark:text-slate-400'
const FALLBACK_DOT = 'bg-slate-400'

/**
 * Status badge for available/reserved/sold. Color never carries meaning
 * alone -- the label text is always present alongside the dot + fill.
 */
export function StatusBadge({ status }: { status: string | null | undefined }) {
  const key = (status ?? '').toLowerCase()
  const badgeClass = STATUS_STYLES[key] ?? FALLBACK_BADGE
  const dotClass = DOT_STYLES[key] ?? FALLBACK_DOT

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${badgeClass}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotClass}`} aria-hidden="true" />
      {status ? status : 'unknown'}
    </span>
  )
}
