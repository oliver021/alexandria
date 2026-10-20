import { describe, expect, it } from 'vitest'
import config from '../vite.config.js'

describe('dev server', () => {
  const proxy = config.server?.proxy ?? {}

  it('sends the REST API to the registry', () => {
    expect(proxy['/api']).toBeTruthy()
  })

  it('sends the description of the API to the same registry, because the Users page reads it', () => {
    expect(proxy['/openapi.json']).toBe(proxy['/api'])
  })

  it('serves the app under /admin/ like the registry does', () => {
    expect(config.base).toBe('/admin/')
  })
})
