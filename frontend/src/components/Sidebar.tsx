import React from 'react'

type SidebarProps = {
  activeView: 'my-schedule' | 'scheduler-view' | 'shift-builder' | 'schedule-blocks' | 'facilities' | 'physicians'
  onSelectView: (view: 'my-schedule' | 'scheduler-view' | 'shift-builder' | 'schedule-blocks' | 'facilities' | 'physicians') => void
}

export default function Sidebar({ activeView, onSelectView }: SidebarProps){
  return (
    <aside className="sidebar">
      <div className="logo">Atlas <span style={{opacity:0.85,fontWeight:500}}>Physician Scheduling</span></div>
      <nav className="nav">
        <button
          type="button"
          className={activeView === 'my-schedule' ? 'active' : ''}
          onClick={() => onSelectView('my-schedule')}
        >
          My Schedule
        </button>
        <button
          type="button"
          className={activeView === 'scheduler-view' ? 'active' : ''}
          onClick={() => onSelectView('scheduler-view')}
        >
          Scheduler View
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
          Schedule Blocks
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
