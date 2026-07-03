import React from 'react'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'
import SchedulerView from './SchedulerView'
import FacilitiesView from './FacilitiesView'

export default function Dashboard() {
  const [activeView, setActiveView] = React.useState<'my-schedule' | 'scheduler-view' | 'facilities'>('my-schedule')
  const [facilitiesRefreshToken, setFacilitiesRefreshToken] = React.useState(0)

  const pageTitle =
    activeView === 'my-schedule'
      ? 'My Schedule'
      : activeView === 'scheduler-view'
        ? 'Scheduler View'
        : 'Facilities'

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
        </main>
      </div>
    </div>
  )
}
