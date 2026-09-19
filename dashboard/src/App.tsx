import { useEffect, useMemo, useState } from 'react'
import { DomainSwitch } from './components/DomainSwitch'
import { KeyWall } from './components/KeyWall'
import { LeadsView } from './components/LeadsView'
import { SignOutSheet } from './components/SignOutSheet'
import { TagReader } from './components/TagReader'
import { TapeCounts } from './components/TapeCounts'
import { DOMAIN_LABEL, normalizeStatus } from './lib/format'
import type { Status } from './lib/format'
import { buildSheetRows } from './lib/sheetRows'
import { useCatalogItems } from './lib/useCatalogItems'
import type { CatalogItem, DomainType } from './lib/useCatalogItems'
import { usePrivateData } from './lib/usePrivateData'
import { useSession } from './lib/useSession'

type View = 'keys' | 'leads'

/** The view lives in the URL hash, so it can be linked to and needs no router or server. */
function readView(): View {
  return window.location.hash.startsWith('#/leads') ? 'leads' : 'keys'
}

function App() {
  const [view, setView] = useState<View>(readView)
  const [domain, setDomain] = useState<DomainType>('cars')
  const [hovered, setHovered] = useState<CatalogItem | null>(null)
  const [pickedId, setPickedId] = useState<number | null>(null)
  // Realtime takes a moment to connect; only call it offline if it has not by then.
  const [connecting, setConnecting] = useState(true)

  useEffect(() => {
    const timer = window.setTimeout(() => setConnecting(false), 6000)
    return () => window.clearTimeout(timer)
  }, [])

  useEffect(() => {
    const onHash = () => setView(readView())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const { items: all, loading, error, connected, changes } = useCatalogItems()
  const { session, ready, signIn, signOut } = useSession()
  const userId = session?.user.id ?? null
  const priv = usePrivateData(userId)

  const items = useMemo(
    () => all.filter((item) => item.domain_type === domain).sort((a, b) => a.odoo_id - b.odoo_id),
    [all, domain],
  )

  const counts = useMemo(() => {
    const tally: Record<Status, number> = { available: 0, reserved: 0, sold: 0 }
    for (const item of items) tally[normalizeStatus(item.status)] += 1
    return tally
  }, [items])

  const sheetRows = useMemo(
    () =>
      buildSheetRows(
        items,
        changes.filter((change) => change.item.domain_type === domain),
        9,
      ),
    [items, changes, domain],
  )

  // Always read the freshest copy of the picked key, so a live change shows.
  const picked = items.find((item) => item.id === pickedId) ?? null
  const reading = hovered ? items.find((item) => item.id === hovered.id) ?? hovered : picked

  function pick(item: CatalogItem) {
    setPickedId((current) => (current === item.id ? null : item.id))
  }

  function switchDomain(next: DomainType) {
    setDomain(next)
    setPickedId(null)
    setHovered(null)
  }

  return (
    <div className="page">
      <header className="masthead">
        <div className="masthead-left">
          <a className="brand" href="#/" aria-label="LeadGate">
            <svg className="brand-mark" viewBox="0 0 24 30" aria-hidden="true">
              <path d="M6 1h12l5 5v22a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V6z" fill="currentColor" />
              <circle cx="12" cy="8.5" r="2.6" fill="var(--enamel)" />
            </svg>
            <span className="brand-name">LeadGate</span>
          </a>
          <nav className="tabs" aria-label="Views">
            <a href="#/" className="tab" aria-current={view === 'keys' ? 'page' : undefined}>
              Keys
            </a>
            <a href="#/leads" className="tab" aria-current={view === 'leads' ? 'page' : undefined}>
              Leads
            </a>
          </nav>
        </div>
        <div className="masthead-right">
          <span className="live" data-connected={connected}>
            <span className="live-dot" aria-hidden="true" />
            {connected ? 'Live' : connecting ? 'Connecting' : 'Offline'}
          </span>
          {session && (
            <button type="button" className="signout" onClick={signOut}>
              Sign out
            </button>
          )}
          {view === 'keys' && <DomainSwitch value={domain} onChange={switchDomain} />}
        </div>
      </header>

      {view === 'leads' ? (
        <LeadsView
          signedIn={ready && session != null}
          leads={priv.leads}
          turns={priv.turns}
          loading={priv.loading}
          error={priv.error}
          onSignIn={signIn}
        />
      ) : (
        <>
          <div className="intro">
            <h1 className="headline">Every key, where it is right now.</h1>
            <div className="intro-side">
              <p className="intro-copy">
                {DOMAIN_LABEL[domain] === 'Cars' ? 'The showroom cabinet.' : 'The listings cabinet.'} Change a
                status in Odoo and the tag moves here about a second later.
              </p>
              <TapeCounts counts={counts} loading={loading} />
            </div>
          </div>

          <main className="board">
            <KeyWall
              key={domain}
              items={items}
              loading={loading}
              error={error}
              selectedId={pickedId}
              onHover={setHovered}
              onSelect={pick}
            />
            <aside className="desk">
              <TagReader item={reading} pinned={!hovered && picked != null} />
              <SignOutSheet rows={sheetRows} loading={loading} />
            </aside>
          </main>
        </>
      )}
    </div>
  )
}

export default App
