import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { API_BASE, setCsrfToken } from '../api'

interface User {
  id: number
  username: string
  email: string
  first_name: string
  last_name: string
  is_staff: boolean
  is_superuser: boolean
  physician_id: number | null
  groups: string[]
  organization_memberships: Array<{
    organization_id: number
    organization_name: string
  }>
  is_org_admin: boolean
  permissions: string[]
  can_manage_schedules: boolean
  can_test_access: boolean
  test_access: {
    domain_id: number
    domain_name: string
    region_id: number
    region_name: string
    role_template_id: number
    role_name: string
    clinically_active: boolean
  } | null
  domain_access: Array<{
    domain_id: number
    domain_name: string
    region_id: number
    region_name: string
    role_template_id: number | null
    role_name: string
    clinically_active: boolean
    active: boolean
    permissions: string[]
  }>
}

interface AuthContextType {
  user: User | null
  isLoading: boolean
  isAuthenticated: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  checkAuth: () => Promise<void>
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  // Check if user is already authenticated on mount
  useEffect(() => {
    checkAuth()
  }, [])

  const checkAuth = async () => {
    try {
      const csrfResponse = await fetch(`${API_BASE}/csrf/`, { credentials: 'include' })
      if (!csrfResponse.ok) throw new Error('Unable to establish a secure browser session')
      const csrfData = await csrfResponse.json()
      setCsrfToken(csrfData.csrfToken || '')
      const response = await fetch(`${API_BASE}/me/`, {
        method: 'GET',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
      })
      
      if (response.ok) {
        const userData = await response.json()
        setUser(userData)
      } else {
        setUser(null)
      }
    } catch (error) {
      console.error('Auth check failed:', error)
      setUser(null)
    } finally {
      setIsLoading(false)
    }
  }

  const login = async (username: string, password: string) => {
    setIsLoading(true)
    try {
      const response = await fetch(`${API_BASE}/login/`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username, password }),
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.error || data.detail || 'Login failed')
      }

      const userData = await response.json()
      setCsrfToken(userData.csrfToken || '')
      setUser(userData)
    } finally {
      setIsLoading(false)
    }
  }

  const logout = async () => {
    setIsLoading(true)
    try {
      // Call logout endpoint to clear server-side session
      // Include CSRF token in header for cross-origin POST request
      const response = await fetch(`${API_BASE}/logout/`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
      })

      // Clear local user state regardless of response
      setUser(null)

      // Verify the session is actually cleared by checking /api/me/
      // If the server didn't clear the session properly, checkAuth will restore it
      await new Promise(resolve => setTimeout(resolve, 100))
      await checkAuth()
    } finally {
      setIsLoading(false)
    }
  }

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: user !== null,
    login,
    logout,
    checkAuth,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
