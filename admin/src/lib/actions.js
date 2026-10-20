import { api } from './api.js'
import { saveBlob, verifyDigest } from './download.js'
import { basename } from './paths.js'

export class IntegrityError extends Error {
  constructor() {
    super('The downloaded file does not match its published checksum, so it was not saved.')
    this.name = 'IntegrityError'
  }
}

/**
 * @param {string} name
 * @param {string} version
 * @returns {Promise<'match' | 'unverified'>}
 */
export async function downloadPackageVersion(name, version) {
  const { blob, sha256 } = await api.downloadPackage(name, version)
  const verdict = await verifyDigest(blob, sha256)
  if (verdict === 'mismatch') throw new IntegrityError()
  saveBlob(blob, `${name}-${version}.zip`)
  return verdict
}

/**
 * @param {string} name
 * @param {string} version
 * @param {string} path
 * @param {string} [expectedSha]
 * @returns {Promise<void>}
 */
export async function downloadPackageFile(name, version, path, expectedSha) {
  const blob = await api.downloadFile(name, version, path)
  if ((await verifyDigest(blob, expectedSha)) === 'mismatch') throw new IntegrityError()
  saveBlob(blob, basename(path))
}
