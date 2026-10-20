import { screen } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FRAMED_MESSAGE } from './lib/framing.js'
import { ADMIN, jsonResponse, mockFetch, resetApp, signIn } from './test/helpers.js'

async function boot({ framed }) {
  vi.resetModules()
  vi.doMock('./lib/framing.js', async (importOriginal) => ({
    ...(await importOriginal()),
    isFramed: () => framed,
  }))
  await import('./main.js')
}

beforeEach(() => {
  resetApp()
  document.body.innerHTML = '<div id="app"></div>'
})

afterEach(() => {
  vi.doUnmock('./lib/framing.js')
  vi.resetModules()
  document.body.innerHTML = ''
  resetApp()
})

describe('main', () => {
  it('mounts the console when the page is the top window of its tab', async () => {
    const http = mockFetch([])
    await boot({ framed: false })
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(document.getElementById('app')).not.toHaveTextContent(FRAMED_MESSAGE)
    expect(http.calls).toHaveLength(0)
  })

  it('shows only a message, and asks the registry for nothing, inside a frame', async () => {
    signIn(ADMIN)
    const http = mockFetch([
      { method: 'GET', path: /.*/, respond: () => jsonResponse({}) },
      { method: 'POST', path: /.*/, respond: () => jsonResponse({}) },
    ])
    await boot({ framed: true })
    const app = document.getElementById('app')
    expect(app).toHaveTextContent(FRAMED_MESSAGE)
    expect(app?.children).toHaveLength(0)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(http.calls).toHaveLength(0)
  })

  it('still refuses to start without its mount point', async () => {
    document.body.innerHTML = ''
    await expect(boot({ framed: false })).rejects.toThrow(/#app element is missing/)
  })
})
