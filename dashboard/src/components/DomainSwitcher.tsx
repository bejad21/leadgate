import type { DomainType } from '../lib/useCatalogItems'

interface DomainSwitcherProps {
  value: DomainType
  onChange: (domain: DomainType) => void
}

const OPTIONS: { value: DomainType; label: string }[] = [
  { value: 'cars', label: 'Cars' },
  { value: 'real_estate', label: 'Real Estate' },
]

/**
 * Segmented Cars / Real Estate toggle. The live proof that one dashboard
 * serves two unrelated catalogs: selecting a domain here filters both
 * LiveLeadsFeed and CatalogStatus client-side.
 */
export function DomainSwitcher({ value, onChange }: DomainSwitcherProps) {
  return (
    <div
      role="group"
      aria-label="Filter catalog by domain"
      className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--code-bg)] p-1"
    >
      {OPTIONS.map((option) => {
        const isActive = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={isActive}
            onClick={() => onChange(option.value)}
            className={`min-h-11 min-w-28 rounded-md px-4 text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)] ${
              isActive
                ? 'bg-[var(--accent)] text-white shadow-sm'
                : 'text-[var(--text)] hover:bg-[var(--accent-bg)]'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
