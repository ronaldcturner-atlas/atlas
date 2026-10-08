import React from 'react'
import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ProtectedRoute } from './contexts/ProtectedRoute'
import Dashboard from './components/Dashboard'
import ChangePassword from './components/ChangePassword'
import { useAuth } from './contexts/AuthContext'

function AuthenticatedApplication() {
  const { user } = useAuth()
  if (user?.must_change_password) return <ChangePassword required />
  return <Dashboard />
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ProtectedRoute>
          <AuthenticatedApplication />
        </ProtectedRoute>
      </AuthProvider>
    </BrowserRouter>
  )
}

