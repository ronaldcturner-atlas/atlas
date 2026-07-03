import React from 'react'
import Sidebar from './Sidebar'
import Topbar from './Topbar'
import Calendar from './Calendar'

export default function Dashboard() {
  return (
    <div className="app-root">
      <Sidebar />
      <div className="main-area">
        <Topbar />
        <main className="content">
          <h1 className="page-title">My Schedule</h1>
          <Calendar />
        </main>
      </div>
    </div>
  )
}
