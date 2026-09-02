import React from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'
import ShiftsView from './ShiftsView'
import ScheduleBuildWorkspace from './ScheduleBuildWorkspace'
import ScheduleBlocksView from './ScheduleBlocksView'
import ScheduleVersionViolationReport from './ScheduleVersionViolationReport'
import ContractsView from './ContractsView'
import FacilitiesView from './FacilitiesView'
import PhysiciansView from './PhysiciansView'
import StatsView from './StatsView'
import { useAuth } from '../contexts/AuthContext'

type AppView = 'my-schedule' | 'stats' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians'

const VIEW_PATHS: Record<AppView, string> = {
  'my-schedule': '/',
  stats: '/stats',
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
  if (pathname === '/shift-builder') {
    return 'shift-builder'
  }
  if (pathname === '/stats') {
    return 'stats'
  }
  if (
    pathname === '/schedule-blocks'
    || /^\/schedule-blocks\/\d+\/requests$/.test(pathname)
    || /^\/schedule-blocks\/\d+\/build$/.test(pathname)
    || /^\/schedule-versions\/\d+\/violations$/.test(pathname)
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
  const { user } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const activeView = viewFromPath(location.pathname) ?? 'my-schedule'
  const requestBuilderMatch = location.pathname.match(/^\/schedule-blocks\/(\d+)\/requests$/)
  const requestBlockId = requestBuilderMatch ? Number(requestBuilderMatch[1]) : null
  const buildWorkspaceMatch = location.pathname.match(/^\/schedule-blocks\/(\d+)\/build$/)
  const buildBlockId = buildWorkspaceMatch ? Number(buildWorkspaceMatch[1]) : null
  const violationReportMatch = location.pathname.match(/^\/schedule-versions\/(\d+)\/violations$/)
  const violationVersionId = violationReportMatch ? Number(violationReportMatch[1]) : null
  const [facilitiesRefreshToken, setFacilitiesRefreshToken] = React.useState(0)
  const [shiftsRefreshToken, setShiftsRefreshToken] = React.useState(0)
  const canManageSchedules = Boolean(
    user?.is_staff
    || user?.is_superuser
    || user?.groups.some((group) => ['admin', 'scheduler'].includes(group.toLowerCase())),
  )

  const pageTitle =
    buildBlockId !== null
      ? 'Schedule Build Workspace'
      : violationVersionId !== null
      ? 'Optimizer Violation Report'
      : requestBlockId !== null
      ? 'Request Builder'
      : activeView === 'my-schedule'
      ? 'Schedule'
      : activeView === 'stats'
      ? 'Stats'
        : activeView === 'shift-builder'
          ? 'Shift Builder'
        : activeView === 'schedule-blocks'
          ? canManageSchedules ? 'Schedule Blocks' : 'My Requests'
        : activeView === 'contracts'
          ? 'Contracts'
        : activeView === 'facilities'
          ? 'Facilities'
          : 'Physicians'

  React.useEffect(() => {
    if (viewFromPath(location.pathname) === null) {
      navigate('/', { replace: true })
      return
    }
    if (!canManageSchedules && (buildBlockId !== null || violationVersionId !== null)) {
      navigate('/schedule-blocks', { replace: true })
    }
  }, [buildBlockId, canManageSchedules, location.pathname, navigate, violationVersionId])

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
          {activeView === 'stats' && <StatsView />}
          {activeView === 'shift-builder' && <ShiftsView />}
          {activeView === 'schedule-blocks' && violationVersionId !== null && (
            <ScheduleVersionViolationReport versionId={violationVersionId} />
          )}
          {activeView === 'schedule-blocks' && violationVersionId === null && buildBlockId !== null && (
            <ScheduleBuildWorkspace
              blockId={buildBlockId}
              onBack={() => navigate('/schedule-blocks')}
            />
          )}
          {activeView === 'schedule-blocks' && violationVersionId === null && buildBlockId === null && (
            <ScheduleBlocksView
              requestUserView={!canManageSchedules}
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
