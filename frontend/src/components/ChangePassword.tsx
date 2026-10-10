import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api'
import { useAuth } from '../contexts/AuthContext'
import { DEFAULT_AUTHENTICATED_PATH } from '../utils/regressionRules'
import './Login.css'

type Props = {
  required?: boolean
  onCancel?: () => void
  onComplete?: () => void
}

function errorMessage(data: any) {
  if (typeof data?.detail === 'string') return data.detail
  const messages = Object.values(data ?? {}).flatMap((value) => (
    Array.isArray(value) ? value.map(String) : typeof value === 'string' ? [value] : []
  ))
  return messages.join(' ') || 'Unable to change password.'
}

export default function ChangePassword({ required = false, onCancel, onComplete }: Props) {
  const { checkAuth, logout } = useAuth()
  const navigate = useNavigate()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setError('')
    try {
      const response = await fetch(`${API_BASE}/password/change/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
          confirm_password: confirmPassword,
        }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(errorMessage(data))
      if (required) navigate(DEFAULT_AUTHENTICATED_PATH, { replace: true })
      await checkAuth()
      onComplete?.()
    } catch (changeError) {
      setError(changeError instanceof Error ? changeError.message : 'Unable to change password.')
    } finally {
      setSaving(false)
    }
  }

  const form = <form onSubmit={submit} className="login-form password-change-form">
    {required && <p className="password-change-message">Change the temporary password before continuing.</p>}
    {error && <div className="login-error">{error}</div>}
    <label className="form-group"><span className="form-label">Current password</span><input className="form-input" type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoFocus /></label>
    <label className="form-group"><span className="form-label">New password</span><input className="form-input" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /><small>Use at least 8 characters and avoid common or personal information.</small></label>
    <label className="form-group"><span className="form-label">Confirm new password</span><input className="form-input" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></label>
    <div className="password-change-actions">
      {!required && <button type="button" className="secondary" onClick={onCancel}>Cancel</button>}
      {required && <button type="button" className="secondary" onClick={() => void logout()}>Logout</button>}
      <button type="submit" className="login-button" disabled={saving || !currentPassword || !newPassword || !confirmPassword}>{saving ? 'Saving...' : 'Change password'}</button>
    </div>
  </form>

  if (!required) return form
  return <div className="login-container"><div className="login-card password-change-card"><div className="login-header"><h1>Set your password</h1></div>{form}</div></div>
}
