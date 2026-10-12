import React from 'react'
import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ProtectedRoute } from './contexts/ProtectedRoute'
import Dashboard from './components/Dashboard'
import ChangePassword from './components/ChangePassword'
import MobileNavigationPreview from './components/MobileNavigationPreview'
import { useAuth } from './contexts/AuthContext'
import { useLocation } from 'react-router-dom'

function AuthenticatedApplication() {
  const { user } = useAuth()
  const location = useLocation()
  if (user?.must_change_password) return <ChangePassword required />
  if (location.pathname === '/mobile-preview') return <MobileNavigationPreview />
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

