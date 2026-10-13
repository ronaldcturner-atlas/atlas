import React from 'react'
import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ProtectedRoute } from './contexts/ProtectedRoute'
import Dashboard from './components/Dashboard'
import ChangePassword from './components/ChangePassword'
import MobileNavigationPreview from './components/MobileNavigationPreview'
import { useAuth } from './contexts/AuthContext'
import { useLocation } from 'react-router-dom'

const MOBILE_APPLICATION_QUERY = '(max-width: 820px), (display-mode: standalone)'

function useMobileApplication() {
  const [isMobileApplication, setIsMobileApplication] = React.useState(() => window.matchMedia(MOBILE_APPLICATION_QUERY).matches)

  React.useEffect(() => {
    const mediaQuery = window.matchMedia(MOBILE_APPLICATION_QUERY)
    const updateApplication = () => setIsMobileApplication(mediaQuery.matches)
    updateApplication()
    mediaQuery.addEventListener('change', updateApplication)
    return () => mediaQuery.removeEventListener('change', updateApplication)
  }, [])

  return isMobileApplication
}

function AuthenticatedApplication() {
  const { user } = useAuth()
  const location = useLocation()
  const isMobileApplication = useMobileApplication()
  if (user?.must_change_password) return <ChangePassword required />
  if (location.pathname === '/mobile-preview' || isMobileApplication) return <MobileNavigationPreview />
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

