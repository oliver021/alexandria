const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
const DASH = '—'

/**
 * @param {number | null | undefined} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return DASH
  if (bytes < 1024) return `${bytes} B`
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2
  const fixed = value.toFixed(digits)
  const text = fixed.includes('.') ? fixed.replace(/\.?0+$/, '') : fixed
  return `${text} ${UNITS[unit]}`
}

/**
 * @param {number | null | undefined} value
 * @returns {string}
 */
export function formatNumber(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return DASH
  return new Intl.NumberFormat('en').format(value)
}

/**
 * @param {number} count
 * @param {string} singular
 * @param {string} [plural]
 * @returns {string}
 */
export function pluralize(count, singular, plural = `${singular}s`) {
  return `${formatNumber(count)} ${count === 1 ? singular : plural}`
}

/**
 * @param {string | null | undefined} value
 * @returns {Date | null}
 */
function toDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/**
 * @typedef {Object} DateOptions
 * @property {string} [locale]
 * @property {string} [timeZone]
 */

/**
 * @param {string | null | undefined} value
 * @param {DateOptions} [options]
 * @returns {string}
 */
export function formatDate(value, { locale, timeZone } = {}) {
  const date = toDate(value)
  if (!date) return DASH
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone }).format(date)
}

/**
 * @param {string | null | undefined} value
 * @param {DateOptions} [options]
 * @returns {string}
 */
export function formatDateTime(value, { locale, timeZone } = {}) {
  const date = toDate(value)
  if (!date) return DASH
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone,
  }).format(date)
}

const RELATIVE_UNITS = /** @type {const} */ ([
  ['year', 31_536_000],
  ['month', 2_592_000],
  ['week', 604_800],
  ['day', 86_400],
  ['hour', 3_600],
  ['minute', 60],
])

/**
 * @param {string | null | undefined} value
 * @param {number} [now]
 * @returns {string}
 */
export function formatRelative(value, now = Date.now()) {
  const date = toDate(value)
  if (!date) return DASH
  const seconds = Math.round((date.getTime() - now) / 1000)
  const absolute = Math.abs(seconds)
  if (absolute < 45) return 'just now'
  const formatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [unit, size] of RELATIVE_UNITS) {
    if (absolute >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(Math.round(seconds / 60), 'minute')
}

/**
 * @param {string | null | undefined} sha
 * @param {number} [length]
 * @returns {string}
 */
export function shortSha(sha, length = 12) {
  if (typeof sha !== 'string' || sha === '') return DASH
  return sha.slice(0, length)
}

const STATUS_LABELS = /** @type {Record<string, string>} */ ({
  draft: 'Draft',
  published: 'Published',
  unpublished: 'Hidden',
  deprecated: 'Deprecated',
})

/**
 * @param {string} status
 * @returns {string}
 */
export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status
}

/**
 * @param {string} kind
 * @returns {string}
 */
export function kindLabel(kind) {
  if (kind === 'skill') return 'Skill'
  if (kind === 'grounding') return 'Grounding'
  return kind
}

/**
 * @param {string} role
 * @returns {string}
 */
export function roleLabel(role) {
  if (role === 'admin') return 'Admin'
  if (role === 'publisher') return 'Publisher'
  return role
}

/**
 * @param {{ revoked_at?: string | null, expires_at?: string | null }} token
 * @param {number} [now]
 * @returns {'revoked' | 'expired' | 'active'}
 */
export function tokenState(token, now = Date.now()) {
  if (token.revoked_at) return 'revoked'
  const expires = toDate(token.expires_at)
  if (expires && expires.getTime() <= now) return 'expired'
  return 'active'
}
