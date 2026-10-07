export function readSessionString(key: string, fallback = '') {
  try {
    return window.sessionStorage.getItem(key) ?? fallback
  } catch {
    return fallback
  }
}

export function readSessionNumber(key: string) {
  const value = readSessionString(key)
  const parsed = Number(value)
  return value && Number.isInteger(parsed) ? parsed : null
}

export function writeSessionSelection(key: string, value: string | number | null) {
  try {
    if (value === null || value === '') window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, String(value))
  } catch {
    // The page still works if browser storage is unavailable.
  }
}
