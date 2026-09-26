import React from 'react'
import { useAuth } from '../contexts/AuthContext'

type TopbarProps = {
  canSwitchView: boolean
  userView: boolean
  onUserViewChange: (userView: boolean) => void
}

export default function Topbar({ canSwitchView, userView, onUserViewChange }: TopbarProps) {
  const { user, logout, isLoading } = useAuth()

  const handleLogout = async () => {
    await logout()
  }

  return (
    <header className="topbar">
      <div style={{ flex: 1 }} />
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {user && (
          <>
            {canSwitchView && (
              <div className="view-mode-toggle" role="group" aria-label="Interface view">
                <button
                  type="button"
                  className={!userView ? 'active' : ''}
                  aria-pressed={!userView}
                  onClick={() => onUserViewChange(false)}
                >
                  Scheduler view
                </button>
                <button
                  type="button"
                  className={userView ? 'active' : ''}
                  aria-pressed={userView}
                  onClick={() => onUserViewChange(true)}
                >
                  User view
                </button>
              </div>
            )}
            {!canSwitchView && <span className="view-mode-label">User view</span>}
            <div style={{ color: 'var(--sidebar-fg)', fontSize: '14px', fontWeight: '500' }}>
              {user.first_name} {user.last_name}
            </div>
            <button
              onClick={handleLogout}
              disabled={isLoading}
              style={{
                padding: '6px 12px',
                background: 'rgba(255, 255, 255, 0.08)',
                border: '1px solid rgba(255, 255, 255, 0.12)',
                borderRadius: '6px',
                color: 'var(--sidebar-fg)',
                cursor: isLoading ? 'not-allowed' : 'pointer',
                fontSize: '12px',
                fontWeight: '500',
                opacity: isLoading ? 0.6 : 1,
                transition: 'all 0.2s ease',
              }}
              onMouseOver={(e) => {
                if (!isLoading) {
                  e.currentTarget.style.background = 'rgba(255, 255, 255, 0.12)'
                }
              }}
              onMouseOut={(e) => {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.08)'
              }}
            >
              Logout
            </button>
          </>
        )}
      </div>
    </header>
  )
}

