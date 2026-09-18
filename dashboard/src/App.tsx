import { useState } from 'react'
import './App.css'
import { CatalogStatus } from './components/CatalogStatus'
import { DomainSwitcher } from './components/DomainSwitcher'
import { LiveLeadsFeed } from './components/LiveLeadsFeed'
import type { DomainType } from './lib/useCatalogItems'

function App() {
  const [domain, setDomain] = useState<DomainType>('cars')

  return (
    <section id="leadgate-dashboard" className="mx-auto w-full max-w-3xl px-4 py-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="!m-0 !text-2xl">LeadGate Dashboard</h1>
        <DomainSwitcher value={domain} onChange={setDomain} />
      </div>
      <div className="mt-6">
        <CatalogStatus domain={domain} />
      </div>
      <div className="mt-6">
        <LiveLeadsFeed domain={domain} />
      </div>
    </section>
  )
}

export default App
