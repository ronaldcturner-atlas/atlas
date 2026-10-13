import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { installApiRequestSecurity } from './api'
import './index.css'

installApiRequestSecurity()

const root = createRoot(document.getElementById('root')!)
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    const applicationScript = Array.from(document.scripts).find((script) => script.type === 'module' && script.src)
    const buildId = applicationScript ? new URL(applicationScript.src).pathname.split('/').pop() : 'atlas'
    void navigator.serviceWorker.register(`/sw.js?build=${encodeURIComponent(buildId || 'atlas')}`).then((registration) => {
      const announceUpdate = () => window.dispatchEvent(new Event('atlas:pwa-update-available'))
      if (registration.waiting) announceUpdate()
      registration.addEventListener('updatefound', () => {
        const worker = registration.installing
        worker?.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) announceUpdate()
        })
      })
    })
  })
}
