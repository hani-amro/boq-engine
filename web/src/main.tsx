import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'

import App from './App'
import { queryClient } from './queryClient'
import { install_unload_guard } from './store'
import './index.css'

install_unload_guard()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        {/* top-center, not a corner: under RTL a corner toast lands where the eye
            is not, and these carry the server's Arabic failure sentences. */}
        <Toaster
          dir="rtl"
          position="top-center"
          richColors
          closeButton
          theme="system"
          toastOptions={{
            style: { fontFamily: 'inherit', textAlign: 'start' },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
