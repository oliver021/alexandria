import { describe, expect, it } from 'vitest'
import {
  formatBytes,
  formatDate,
  formatDateTime,
  formatNumber,
  formatRelative,
  kindLabel,
  pluralize,
  roleLabel,
  shortSha,
  statusLabel,
  tokenState,
} from './format.js'

describe('formatBytes', () => {
  it('uses binary units and trims useless zeros', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1024)).toBe('1 KiB')
    expect(formatBytes(1536)).toBe('1.5 KiB')
    expect(formatBytes(262144)).toBe('256 KiB')
    expect(formatBytes(26214400)).toBe('25 MiB')
    expect(formatBytes(5 * 1024 ** 3)).toBe('5 GiB')
  })

  it('does not strip the zeros of whole numbers', () => {
    expect(formatBytes(100 * 1024 ** 2)).toBe('100 MiB')
    expect(formatBytes(10 * 1024)).toBe('10 KiB')
    expect(formatBytes(200 * 1024)).toBe('200 KiB')
    expect(formatBytes(1000 * 1024)).toBe('1000 KiB')
  })

  it('rounds to a readable precision', () => {
    expect(formatBytes(8168)).toBe('7.98 KiB')
    expect(formatBytes(15.3 * 1024 ** 2)).toBe('15.3 MiB')
  })

  it('falls back to a dash for invalid input', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(undefined)).toBe('—')
    expect(formatBytes(-1)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
  })
})

describe('numbers and plurals', () => {
  it('groups thousands', () => {
    expect(formatNumber(2000)).toBe('2,000')
    expect(formatNumber(null)).toBe('—')
  })

  it('pluralizes', () => {
    expect(pluralize(1, 'file')).toBe('1 file')
    expect(pluralize(0, 'file')).toBe('0 files')
    expect(pluralize(2000, 'entry', 'entries')).toBe('2,000 entries')
  })
})

describe('dates', () => {
  const options = { locale: 'en-US', timeZone: 'UTC' }

  it('formats dates and date times for a fixed locale', () => {
    expect(formatDate('2026-03-05T23:30:00Z', options)).toBe('Mar 5, 2026')
    expect(formatDateTime('2026-03-05T23:30:00Z', options)).toMatch(/Mar 5, 2026.*11:30\sPM/)
  })

  it('shows a dash for missing or invalid dates', () => {
    expect(formatDate(null)).toBe('—')
    expect(formatDate('not a date')).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
    expect(formatRelative('')).toBe('—')
  })

  it('describes relative times', () => {
    const now = Date.parse('2026-03-10T12:00:00Z')
    expect(formatRelative('2026-03-10T11:59:50Z', now)).toBe('just now')
    expect(formatRelative('2026-03-10T11:55:00Z', now)).toBe('5 minutes ago')
    expect(formatRelative('2026-03-10T09:00:00Z', now)).toBe('3 hours ago')
    expect(formatRelative('2026-03-09T12:00:00Z', now)).toBe('yesterday')
    expect(formatRelative('2026-02-03T12:00:00Z', now)).toBe('last month')
    expect(formatRelative('2024-03-10T12:00:00Z', now)).toBe('2 years ago')
    expect(formatRelative('2026-03-11T12:00:00Z', now)).toBe('tomorrow')
  })
})

describe('labels', () => {
  it('maps API values to interface labels', () => {
    expect(statusLabel('unpublished')).toBe('Hidden')
    expect(statusLabel('deprecated')).toBe('Deprecated')
    expect(statusLabel('other')).toBe('other')
    expect(kindLabel('grounding')).toBe('Grounding')
    expect(roleLabel('publisher')).toBe('Publisher')
    expect(roleLabel('admin')).toBe('Admin')
  })

  it('shortens hashes', () => {
    expect(shortSha('0123456789abcdef0123')).toBe('0123456789ab')
    expect(shortSha('')).toBe('—')
    expect(shortSha(null)).toBe('—')
  })
})

describe('tokenState', () => {
  const now = Date.parse('2026-03-10T12:00:00Z')

  it('reports revoked, expired and active tokens', () => {
    expect(tokenState({ revoked_at: '2026-03-01T00:00:00Z', expires_at: null }, now)).toBe('revoked')
    expect(tokenState({ revoked_at: null, expires_at: '2026-03-09T00:00:00Z' }, now)).toBe('expired')
    expect(tokenState({ revoked_at: null, expires_at: '2026-04-09T00:00:00Z' }, now)).toBe('active')
    expect(tokenState({ revoked_at: null, expires_at: null }, now)).toBe('active')
  })

  it('treats revocation as stronger than expiry', () => {
    expect(
      tokenState({ revoked_at: '2026-03-01T00:00:00Z', expires_at: '2026-03-02T00:00:00Z' }, now),
    ).toBe('revoked')
  })
})
