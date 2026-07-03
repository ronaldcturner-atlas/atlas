import React from 'react'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'
import SchedulerView from './SchedulerView'
import ShiftsView from './ShiftsView'
import FacilitiesView from './FacilitiesView'
import PhysiciansView from './PhysiciansView'

export default function Dashboard() {
  const [activeView, setActiveView] = React.useState<'my-schedule' | 'scheduler-view' | 'shift-builder' | 'facilities' | 'physicians'>('my-schedule')
  const [facilitiesRefreshToken, setFacilitiesRefreshToken] = React.useState(0)
  const [shiftsRefreshToken, setShiftsRefreshToken] = React.useState(0)

  const pageTitle =
    activeView === 'my-schedule'
      ? 'My Schedule'
      : activeView === 'scheduler-view'
        ? 'Scheduler View'
        : activeView === 'shift-builder'
          ? 'Shift Builder'
        : activeView === 'facilities'
          ? 'Facilities'
          : 'Physicians'

  const handleFacilitiesChanged = React.useCallback(() => {
    setFacilitiesRefreshToken((current) => current + 1)
  }, [])

  return (
    <div className="app-root">
      <Sidebar activeView={activeView} onSelectView={setActiveView} />
      <div className="main-area">
        <Topbar />
        <main className="content">
          <h1 className="page-title">{pageTitle}</h1>
          {activeView === 'my-schedule' && <Calendar shiftsRefreshToken={shiftsRefreshToken} />}
          {activeView === 'scheduler-view' && (
            <SchedulerView
              facilitiesRefreshToken={facilitiesRefreshToken}
              shiftsRefreshToken={shiftsRefreshToken}
            />
          )}
          {activeView === 'shift-builder' && <ShiftsView />}
          {activeView === 'facilities' && (
            <FacilitiesView onFacilitiesChanged={handleFacilitiesChanged} />
          )}
          {activeView === 'physicians' && <PhysiciansView />}
        </main>
      </div>
    </div>
  )
}
