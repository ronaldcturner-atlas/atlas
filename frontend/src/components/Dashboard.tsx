import React from 'react'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'
import SchedulerView from './SchedulerView'
import FacilitiesView from './FacilitiesView'
import PhysiciansView from './PhysiciansView'

export default function Dashboard() {
  const [activeView, setActiveView] = React.useState<'my-schedule' | 'scheduler-view' | 'facilities' | 'physicians'>('my-schedule')
  const [facilitiesRefreshToken, setFacilitiesRefreshToken] = React.useState(0)

  const pageTitle =
    activeView === 'my-schedule'
      ? 'My Schedule'
      : activeView === 'scheduler-view'
        ? 'Scheduler View'
        : activeView === 'facilities'
          ? 'Facilities'
          : 'Physicians'

  return (
    <div className="app-root">
      <Sidebar activeView={activeView} onSelectView={setActiveView} />
      <div className="main-area">
        <Topbar />
        <main className="content">
          <h1 className="page-title">{pageTitle}</h1>
          {activeView === 'my-schedule' && <Calendar />}
          {activeView === 'scheduler-view' && <SchedulerView facilitiesRefreshToken={facilitiesRefreshToken} />}
          {activeView === 'facilities' && (
            <FacilitiesView onFacilitiesChanged={() => setFacilitiesRefreshToken((current) => current + 1)} />
          )}
          {activeView === 'physicians' && <PhysiciansView />}
        </main>
      </div>
    </div>
  )
}
