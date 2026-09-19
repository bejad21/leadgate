import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  // GitHub Pages serves a project site from /<repo>/, so the deploy workflow sets
  // VITE_BASE=/leadgate/. Locally it stays at the root.
  base: process.env.VITE_BASE ?? '/',
  plugins: [react(), tailwindcss()],
})
