import React from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'
import SchedulerView from './SchedulerView'
import ShiftsView from './ShiftsView'
import ScheduleBuildWorkspace from './ScheduleBuildWorkspace'
import ScheduleBlocksView from './ScheduleBlocksView'
import ContractsView from './ContractsView'
import FacilitiesView from './FacilitiesView'
import PhysiciansView from './PhysiciansView'

type AppView = 'my-schedule' | 'scheduler-view' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians'

const VIEW_PATHS: Record<AppView, string> = {
  'my-schedule': '/',
  'scheduler-view': '/scheduler',
  'shift-builder': '/shift-builder',
  'schedule-blocks': '/schedule-blocks',
  contracts: '/contracts',
  facilities: '/facilities',
  physicians: '/physicians',
}

function viewFromPath(pathname: string): AppView | null {
  if (pathname === '/') {
    return 'my-schedule'
  }
  if (pathname === '/scheduler') {
    return 'scheduler-view'
  }
  if (pathname === '/shift-builder') {
    return 'shift-builder'
  }
  if (
    pathname === '/schedule-blocks'
    || /^\/schedule-blocks\/\d+\/requests$/.test(pathname)
    || /^\/schedule-blocks\/\d+\/build$/.test(pathname)
  ) {
    return 'schedule-blocks'
  }
  if (pathname === '/contracts') {
    return 'contracts'
  }
  if (pathname === '/facilities') {
    return 'facilities'
  }
  if (pathname === '/physicians') {
    return 'physicians'
  }
  return null
}

export default function Dashboard() {
  const location = useLocation()
  const navigate = useNavigate()
  const activeView = viewFromPath(location.pathname) ?? 'my-schedule'
  const requestBuilderMatch = location.pathname.match(/^\/schedule-blocks\/(\d+)\/requests$/)
  const requestBlockId = requestBuilderMatch ? Number(requestBuilderMatch[1]) : null
  const buildWorkspaceMatch = location.pathname.match(/^\/schedule-blocks\/(\d+)\/build$/)
  const buildBlockId = buildWorkspaceMatch ? Number(buildWorkspaceMatch[1]) : null
  const [facilitiesRefreshToken, setFacilitiesRefreshToken] = React.useState(0)
  const [shiftsRefreshToken, setShiftsRefreshToken] = React.useState(0)

  const pageTitle =
    buildBlockId !== null
      ? 'Schedule Build Workspace'
      : requestBlockId !== null
      ? 'Request Builder'
      : activeView === 'my-schedule'
      ? 'My Schedule'
      : activeView === 'scheduler-view'
        ? 'Scheduler View'
        : activeView === 'shift-builder'
          ? 'Shift Builder'
        : activeView === 'schedule-blocks'
          ? 'Schedule Blocks'
        : activeView === 'contracts'
          ? 'Contracts'
        : activeView === 'facilities'
          ? 'Facilities'
          : 'Physicians'

  React.useEffect(() => {
    if (viewFromPath(location.pathname) === null) {
      navigate('/', { replace: true })
    }
  }, [location.pathname, navigate])

  const handleFacilitiesChanged = React.useCallback(() => {
    setFacilitiesRefreshToken((current) => current + 1)
  }, [])

  return (
    <div className="app-root">
      <Sidebar
        activeView={activeView}
        onSelectView={(view) => navigate(VIEW_PATHS[view])}
      />
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
          {activeView === 'schedule-blocks' && buildBlockId !== null && (
            <ScheduleBuildWorkspace
              blockId={buildBlockId}
              onBack={() => navigate('/schedule-blocks')}
            />
          )}
          {activeView === 'schedule-blocks' && buildBlockId === null && (
            <ScheduleBlocksView
              requestBlockId={requestBlockId}
              onOpenRequests={(blockId) => navigate(`/schedule-blocks/${blockId}/requests`)}
              onCloseRequests={() => navigate('/schedule-blocks')}
              onOpenBuild={(blockId) => navigate(`/schedule-blocks/${blockId}/build`)}
            />
          )}
          {activeView === 'contracts' && <ContractsView />}
          {activeView === 'facilities' && (
            <FacilitiesView onFacilitiesChanged={handleFacilitiesChanged} />
          )}
          {activeView === 'physicians' && <PhysiciansView />}
        </main>
      </div>
    </div>
  )
}
