const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim()

export const API_BASE = (configuredApiBase || '/api').replace(/\/$/, '')

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE'])
let currentCsrfToken = ''

function csrfToken(): string {
  if (currentCsrfToken) return currentCsrfToken
  const token = document.cookie
    .split(';')
    .map(value => value.trim())
    .find(value => value.startsWith('csrftoken='))
  return token ? decodeURIComponent(token.slice('csrftoken='.length)) : ''
}
export function setCsrfToken(token: string): void {
  currentCsrfToken = token
}

function isAtlasApiRequest(input: RequestInfo | URL): boolean {
  const requestUrl = new URL(
    input instanceof Request ? input.url : String(input),
    window.location.origin,
  )
  const apiUrl = new URL(`${API_BASE}/`, window.location.origin)
  return requestUrl.origin === apiUrl.origin && requestUrl.pathname.startsWith(apiUrl.pathname)
}

export function installApiRequestSecurity(): void {
  const originalFetch = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
    if (!isAtlasApiRequest(input)) {
      return originalFetch(input, init)
    }

    const requestMethod = input instanceof Request ? input.method : 'GET'
    const method = (init.method || requestMethod).toUpperCase()
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init.headers).forEach((value, key) => headers.set(key, value))
    if (!SAFE_METHODS.has(method)) {
      const token = csrfToken()
      if (token) headers.set('X-CSRFToken', token)
    }

    return originalFetch(input, {
      ...init,
      credentials: init.credentials || 'include',
      headers,
    })
  }
}
