import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  // Set VITE_BASE to serve the build from a sub-path. Locally it stays at the root.
  base: process.env.VITE_BASE ?? '/',
  plugins: [react(), tailwindcss()],
})
