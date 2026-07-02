import React from 'react'

export default function Topbar(){
  return (
    <header className="topbar">
      <div style={{flex:1}} />
      <div style={{display:'flex',gap:12,alignItems:'center'}}>
        <div style={{color:'var(--muted)'}}>No user</div>
      </div>
    </header>
  )
}
