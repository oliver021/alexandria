import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { svelteTesting } from '@testing-library/svelte/vite'
import { CONTENT_SECURITY_POLICY } from './csp.js'

const api = process.env.ALEXANDRIA_API || 'http://localhost:8000'

function contentSecurityPolicy() {
  return {
    name: 'alexandria-content-security-policy',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: CONTENT_SECURITY_POLICY },
          injectTo: 'head-prepend',
        },
      ]
    },
  }
}

export default defineConfig({
  plugins: [svelte(), svelteTesting(), contentSecurityPolicy()],
  base: '/admin/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': api,
      '/openapi.json': api,
    },
  },
  test: {
    environment: 'jsdom',
    // `browser/` and `scripts/` run in plain Node: their files say so with `@vitest-environment node`.
    include: ['src/**/*.test.js', 'browser/**/*.test.js', 'scripts/**/*.test.js'],
    setupFiles: ['./src/test/setup.js'],
    restoreMocks: true,
  },
})
