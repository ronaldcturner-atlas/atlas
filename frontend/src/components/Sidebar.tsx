import React from 'react'

type SidebarProps = {
  activeView: 'my-schedule' | 'stats' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians'
  onSelectView: (view: 'my-schedule' | 'stats' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians') => void
  userView: boolean
}

export default function Sidebar({ activeView, onSelectView, userView }: SidebarProps){
  const [collapsed, setCollapsed] = React.useState(() => (
    window.localStorage.getItem('atlas-sidebar-collapsed') === 'true'
  ))
  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current
      window.localStorage.setItem('atlas-sidebar-collapsed', String(next))
      return next
    })
  }
  return (
    <aside className={`sidebar${collapsed ? ' sidebar-collapsed' : ''}`}>
      <div className="sidebar-header">
        <div className="logo">
          <span className="logo-mark">
            <img src="/atlas-logo.png" alt="" aria-hidden="true" />
          </span>
          <span className="logo-copy"><strong>Atlas</strong><small>Scheduling Platform</small></span>
        </div>
        <button
          type="button"
          className="sidebar-collapse-button"
          onClick={toggleCollapsed}
          aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
        >
          {collapsed ? '›' : '‹'}
        </button>
      </div>
      <nav className="nav">
        <button
          type="button"
          className={activeView === 'my-schedule' ? 'active' : ''}
          onClick={() => onSelectView('my-schedule')}
          title="My Schedule"
        >
          <span className="nav-icon" aria-hidden="true">▦</span>
          <span className="nav-label">My Schedule</span>
        </button>
        <button
          type="button"
          className={activeView === 'stats' ? 'active' : ''}
          onClick={() => onSelectView('stats')}
          title="Stats"
        >
          <span className="nav-icon" aria-hidden="true">▥</span>
          <span className="nav-label">Stats</span>
        </button>
        {!userView && (
          <>
        <button
          type="button"
          className={activeView === 'shift-builder' ? 'active' : ''}
          onClick={() => onSelectView('shift-builder')}
          title="Shift Builder"
        >
          <span className="nav-icon" aria-hidden="true">✦</span>
          <span className="nav-label">Shift Builder</span>
        </button>
          </>
        )}
        <button
          type="button"
          className={activeView === 'schedule-blocks' ? 'active' : ''}
          onClick={() => onSelectView('schedule-blocks')}
          title="Schedule Blocks"
        >
          <span className="nav-icon" aria-hidden="true">▤</span>
          <span className="nav-label">Schedule Blocks</span>
        </button>
        {!userView && (
          <>
        <button
          type="button"
          className={activeView === 'contracts' ? 'active' : ''}
          onClick={() => onSelectView('contracts')}
          title="Contracts"
        >
          <span className="nav-icon" aria-hidden="true">≡</span>
          <span className="nav-label">Contracts</span>
        </button>
        <button
          type="button"
          className={activeView === 'facilities' ? 'active' : ''}
          onClick={() => onSelectView('facilities')}
          title="Facilities"
        >
          <span className="nav-icon" aria-hidden="true">⌂</span>
          <span className="nav-label">Facilities</span>
        </button>
        <button
          type="button"
          className={activeView === 'physicians' ? 'active' : ''}
          onClick={() => onSelectView('physicians')}
          title="Physicians"
        >
          <span className="nav-icon" aria-hidden="true">●</span>
          <span className="nav-label">Physicians</span>
        </button>
          </>
        )}
      </nav>
    </aside>
  )
}
