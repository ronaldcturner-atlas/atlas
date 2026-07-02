import React from 'react'

export default function Sidebar(){
  return (
    <aside className="sidebar">
      <div className="logo">Atlas <span style={{opacity:0.85,fontWeight:500}}>Physician Scheduling</span></div>
      <nav className="nav">
        <a className="active" href="#">My Schedule</a>
      </nav>
    </aside>
  )
}
