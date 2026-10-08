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
