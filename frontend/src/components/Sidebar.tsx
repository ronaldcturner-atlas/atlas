import React from 'react'
import { useAuth } from '../contexts/AuthContext'

type SidebarProps = {
  activeView: 'my-schedule' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians'
  onSelectView: (view: 'my-schedule' | 'shift-builder' | 'schedule-blocks' | 'contracts' | 'facilities' | 'physicians') => void
}

export default function Sidebar({ activeView, onSelectView }: SidebarProps){
  const { user } = useAuth()
  const canManageSchedules = Boolean(
    user?.is_staff
    || user?.is_superuser
    || user?.groups.some((group) => ['admin', 'scheduler'].includes(group.toLowerCase())),
  )
  return (
    <aside className="sidebar">
      <div className="logo">Atlas <span style={{opacity:0.85,fontWeight:500}}>Physician Scheduling</span></div>
      <nav className="nav">
        <button
          type="button"
          className={activeView === 'my-schedule' ? 'active' : ''}
          onClick={() => onSelectView('my-schedule')}
        >
          Schedule
        </button>
        <button
          type="button"
          className={activeView === 'shift-builder' ? 'active' : ''}
          onClick={() => onSelectView('shift-builder')}
        >
          Shift Builder
        </button>
        <button
          type="button"
          className={activeView === 'schedule-blocks' ? 'active' : ''}
          onClick={() => onSelectView('schedule-blocks')}
        >
          {canManageSchedules ? 'Schedule Blocks' : 'My Requests'}
        </button>
        <button
          type="button"
          className={activeView === 'contracts' ? 'active' : ''}
          onClick={() => onSelectView('contracts')}
        >
          Contracts
        </button>
        <button
          type="button"
          className={activeView === 'facilities' ? 'active' : ''}
          onClick={() => onSelectView('facilities')}
        >
          Facilities
        </button>
        <button
          type="button"
          className={activeView === 'physicians' ? 'active' : ''}
          onClick={() => onSelectView('physicians')}
        >
          Physicians
        </button>
      </nav>
    </aside>
  )
}
