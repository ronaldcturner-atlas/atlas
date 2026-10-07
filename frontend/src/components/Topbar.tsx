import React from 'react'
import { useAuth } from '../contexts/AuthContext'

type TopbarProps = {
  canSwitchView: boolean
  userView: boolean
  onUserViewChange: (userView: boolean) => void
}

export default function Topbar({ canSwitchView, userView, onUserViewChange }: TopbarProps) {
  const { user, logout, isLoading, checkAuth } = useAuth()
  const [showAccessTester, setShowAccessTester] = React.useState(false)
  const [testOptions, setTestOptions] = React.useState<{ domains: Array<{ id: number; name: string; region_id: number; region_name: string; organization_name: string }>; roles: Array<{ id: number; name: string; region_id: number }> } | null>(null)
  const [testDomainId, setTestDomainId] = React.useState<number | ''>('')
  const [testRoleId, setTestRoleId] = React.useState<number | ''>('')
  const [testClinicallyActive, setTestClinicallyActive] = React.useState(true)
  const [testError, setTestError] = React.useState('')
  const [testSaving, setTestSaving] = React.useState(false)

  const handleLogout = async () => {
    await logout()
  }

  const openAccessTester = async () => {
    setTestError('')
    const response = await fetch('http://localhost:8000/api/development/role-test/', { credentials: 'include' })
    if (!response.ok) {
      setTestError('Unable to load testing options.')
      setShowAccessTester(true)
      return
    }
    const options = await response.json()
    setTestOptions(options)
    const initialDomainId = user?.test_access?.domain_id ?? options.domains[0]?.id ?? ''
    const initialDomain = options.domains.find((domain: { id: number }) => domain.id === initialDomainId)
    const initialRoleId = user?.test_access?.role_template_id
      ?? options.roles.find((role: { region_id: number }) => role.region_id === initialDomain?.region_id)?.id
      ?? ''
    setTestDomainId(initialDomainId)
    setTestRoleId(initialRoleId)
    setTestClinicallyActive(user?.test_access?.clinically_active ?? true)
    setShowAccessTester(true)
  }

  const selectedTestDomain = testOptions?.domains.find((domain) => domain.id === testDomainId)
  const availableTestRoles = testOptions?.roles.filter((role) => role.region_id === selectedTestDomain?.region_id) ?? []

  const startAccessTest = async () => {
    if (!testDomainId || !testRoleId) return
    setTestSaving(true)
    setTestError('')
    try {
      const response = await fetch('http://localhost:8000/api/development/role-test/', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          domain_id: testDomainId,
          role_template_id: testRoleId,
          clinically_active: testClinicallyActive,
        }),
      })
      if (!response.ok) throw new Error('Unable to start access test.')
      setShowAccessTester(false)
      await checkAuth()
    } catch (error) {
      setTestError(error instanceof Error ? error.message : 'Unable to start access test.')
    } finally {
      setTestSaving(false)
    }
  }

  const exitAccessTest = async () => {
    setTestSaving(true)
    try {
      await fetch('http://localhost:8000/api/development/role-test/', {
        method: 'DELETE',
        credentials: 'include',
      })
      await checkAuth()
    } finally {
      setTestSaving(false)
    }
  }

  return (
    <header className="topbar">
      <div style={{ flex: 1 }} />
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {user && (
          <>
            {user.test_access ? (
              <div className="test-access-banner">
                <span>Testing {user.test_access.role_name} · {user.test_access.region_name} / {user.test_access.domain_name} · {user.test_access.clinically_active ? 'Clinically active' : 'Not clinically active'}</span>
                <button type="button" onClick={exitAccessTest} disabled={testSaving}>Exit test</button>
              </div>
            ) : user.can_test_access ? (
              <button type="button" className="test-access-open" onClick={openAccessTester}>Test access</button>
            ) : null}
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
      {showAccessTester && <div className="shift-modal-overlay" onClick={() => setShowAccessTester(false)}>
        <div className="shift-modal test-access-modal" onClick={(event) => event.stopPropagation()}>
          <div className="shift-modal-header"><h2>Test Role Access</h2><p>Development testing only. Your saved access will not change.</p></div>
          <div className="shift-modal-body">
            {testError && <div className="organization-error">{testError}</div>}
            <label><span>Region and Domain</span><select value={testDomainId} onChange={(event) => {
              const nextDomainId = Number(event.target.value)
              const nextDomain = testOptions?.domains.find((domain) => domain.id === nextDomainId)
              setTestDomainId(nextDomainId)
              setTestRoleId(testOptions?.roles.find((role) => role.region_id === nextDomain?.region_id)?.id ?? '')
            }}>{testOptions?.domains.map((domain) => <option key={domain.id} value={domain.id}>{domain.region_name} / {domain.name}</option>)}</select></label>
            <label><span>Role</span><select value={testRoleId} onChange={(event) => setTestRoleId(Number(event.target.value))}>{availableTestRoles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label>
            <label className="test-access-clinical"><input type="checkbox" checked={testClinicallyActive} onChange={(event) => setTestClinicallyActive(event.target.checked)} /><span>Clinically active in this Domain</span></label>
          </div>
          <div className="shift-modal-actions"><button type="button" className="secondary" onClick={() => setShowAccessTester(false)}>Cancel</button><button type="button" onClick={startAccessTest} disabled={testSaving || !testDomainId || !testRoleId}>{testSaving ? 'Starting…' : 'Start Test'}</button></div>
        </div>
      </div>}
    </header>
  )
}

