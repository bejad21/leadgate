import type { DomainType } from '../lib/useCatalogItems'
import { DOMAIN_LABEL } from '../lib/format'

interface DomainSwitchProps {
  value: DomainType
  onChange: (domain: DomainType) => void
}

const DOMAINS: DomainType[] = ['cars', 'real_estate']

/** A two-position brass switch plate. */
export function DomainSwitch({ value, onChange }: DomainSwitchProps) {
  return (
    <div className="switch" role="radiogroup" aria-label="Catalog" data-position={DOMAINS.indexOf(value)}>
      <span className="switch-thumb" aria-hidden="true" />
      {DOMAINS.map((domain) => (
        <button
          key={domain}
          type="button"
          role="radio"
          aria-checked={value === domain}
          className="switch-option"
          onClick={() => onChange(domain)}
        >
          {DOMAIN_LABEL[domain]}
        </button>
      ))}
    </div>
  )
}
