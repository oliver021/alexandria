import { safeFilename } from './paths.js'

const REVOKE_DELAY_MS = 30_000

/**
 * @param {Blob} blob
 * @param {string} filename
 */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = safeFilename(filename)
  link.rel = 'noopener'
  link.hidden = true
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS)
}

/**
 * @param {Blob} blob
 * @returns {Promise<string | null>}
 */
export async function sha256Hex(blob) {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  const digest = await subtle.digest('SHA-256', await blob.arrayBuffer())
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * @param {Blob} blob
 * @param {string | null | undefined} expected
 * @returns {Promise<'match' | 'mismatch' | 'unverified'>}
 */
export async function verifyDigest(blob, expected) {
  if (!expected) return 'unverified'
  const actual = await sha256Hex(blob)
  if (actual === null) return 'unverified'
  return actual === expected.toLowerCase() ? 'match' : 'mismatch'
}
