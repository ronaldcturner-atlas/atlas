import React from 'react'
import Sidebar from './components/Sidebar'
import Topbar from './components/Topbar'
import Calendar from './components/Calendar'

export default function App() {
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
