import React from 'react'

type SidebarProps = {
  activeView: 'my-schedule' | 'stats' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians' | 'roles' | 'organization'
  onSelectView: (view: 'my-schedule' | 'stats' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians' | 'roles' | 'organization') => void
  userView: boolean
  canManageOrganization: boolean
  canViewRoles: boolean
  canManageShiftTemplates: boolean
  canManageFacilities: boolean
  canManageContracts: boolean
}

export default function Sidebar({ activeView, onSelectView, userView, canManageOrganization, canViewRoles, canManageShiftTemplates, canManageFacilities, canManageContracts }: SidebarProps){
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
          title="Schedule"
        >
          <span className="nav-icon" aria-hidden="true">▦</span>
          <span className="nav-label">Schedule</span>
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
        {!userView && canManageShiftTemplates && <button
          type="button"
          className={activeView === 'shift-builder' ? 'active' : ''}
          onClick={() => onSelectView('shift-builder')}
          title="Shift Builder"
        >
          <span className="nav-icon" aria-hidden="true">✦</span>
          <span className="nav-label">Shift Builder</span>
        </button>}
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
        {canManageContracts && <button
          type="button"
          className={activeView === 'contracts' ? 'active' : ''}
          onClick={() => onSelectView('contracts')}
          title="Contracts"
        >
          <span className="nav-icon" aria-hidden="true">≡</span>
          <span className="nav-label">Contracts</span>
        </button>}
        {canManageFacilities && <button
          type="button"
          className={activeView === 'facilities' ? 'active' : ''}
          onClick={() => onSelectView('facilities')}
          title="Facilities"
        >
          <span className="nav-icon" aria-hidden="true">⌂</span>
          <span className="nav-label">Facilities</span>
        </button>}
        {canViewRoles && <button
          type="button"
          className={activeView === 'roles' ? 'active' : ''}
          onClick={() => onSelectView('roles')}
          title="Roles"
        >
          <span className="nav-icon" aria-hidden="true">◆</span>
          <span className="nav-label">Roles</span>
        </button>}
        {canManageOrganization && (
          <button
            type="button"
            className={activeView === 'organization' ? 'active' : ''}
            onClick={() => onSelectView('organization')}
            title="Organization"
          >
            <span className="nav-icon" aria-hidden="true">◎</span>
            <span className="nav-label">Organization</span>
          </button>
        )}
          </>
        )}
        <button
          type="button"
          className={activeView === 'physicians' ? 'active' : ''}
          onClick={() => onSelectView('physicians')}
          title="Users"
        >
          <span className="nav-icon" aria-hidden="true">●</span>
          <span className="nav-label">Users</span>
        </button>
      </nav>
      {activeView === 'my-schedule' && (
        <section className="sidebar-shift-legend" aria-label="Shift colors">
          <h2>Shift colors</h2>
          <div><span className="sidebar-legend-swatch sidebar-legend-own" aria-hidden="true" />My shifts</div>
          <div><span className="sidebar-legend-swatch sidebar-legend-available" aria-hidden="true" />Available pickup</div>
          <div><span className="sidebar-legend-swatch sidebar-legend-posted" aria-hidden="true" />My posted shift</div>
          <div><span className="sidebar-legend-swatch sidebar-legend-trade-sent" aria-hidden="true" />Sent trade</div>
          <div><span className="sidebar-legend-swatch sidebar-legend-trade-received" aria-hidden="true" />Received trade</div>
          <div><span className="sidebar-legend-swatch sidebar-legend-open" aria-hidden="true" />Open shift</div>
        </section>
      )}
    </aside>
  )
}
