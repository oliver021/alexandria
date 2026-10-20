import { describe, expect, it } from 'vitest'
import { ApiError } from './api.js'
import { PACKAGE_REASONS, describeError, describeUploadError } from './reasons.js'

const CONTRACT_REASONS = [
  'NOT_A_ZIP',
  'CORRUPT_ARCHIVE',
  'ENCRYPTED_ENTRY',
  'UNSUPPORTED_COMPRESSION',
  'SYMLINK_NOT_ALLOWED',
  'UNSAFE_PATH',
  'DUPLICATE_PATH',
  'TOO_MANY_FILES',
  'TOO_LARGE',
  'MISSING_ENTRYPOINT',
  'AMBIGUOUS_ENTRYPOINT',
  'BAD_FRONTMATTER',
  'MISSING_FIELD',
  'INVALID_FIELD',
  'KIND_MISMATCH',
  'VERSION_MISMATCH',
]

function apiError(status, code, message, reason = null) {
  return new ApiError({ status, code, message, reason })
}

describe('PACKAGE_REASONS', () => {
  it('explains every reason of the package contract and nothing else', () => {
    expect(Object.keys(PACKAGE_REASONS).sort()).toEqual([...CONTRACT_REASONS].sort())
  })

  it.each(CONTRACT_REASONS)('gives %s a title, an explanation and a fix', (reason) => {
    const entry = PACKAGE_REASONS[reason]
    for (const field of [entry.title, entry.explanation, entry.fix]) {
      expect(typeof field).toBe('string')
      expect(field.trim().length).toBeGreaterThan(10)
    }
  })

  it('is frozen', () => {
    expect(Object.isFrozen(PACKAGE_REASONS)).toBe(true)
  })
})

describe('describeUploadError', () => {
  it.each(CONTRACT_REASONS)('turns INVALID_PACKAGE/%s into a human explanation', (reason) => {
    const described = describeUploadError(
      apiError(422, 'INVALID_PACKAGE', `Invalid package: ${reason}`, reason),
    )
    expect(described.title).toBe(PACKAGE_REASONS[reason].title)
    expect(described.message).toBe(PACKAGE_REASONS[reason].explanation)
    expect(described.hint).toBe(PACKAGE_REASONS[reason].fix)
    expect(described.reason).toBe(reason)
    expect(described.serverMessage).toBe(`Invalid package: ${reason}`)
  })

  it('falls back to the server message for an unknown reason', () => {
    const described = describeUploadError(
      apiError(422, 'INVALID_PACKAGE', 'Something new went wrong', 'FUTURE_REASON'),
    )
    expect(described.title).toBe('The package is not valid')
    expect(described.message).toBe('Something new went wrong')
  })

  it('mentions the configured size limit when the upload is too large', () => {
    const described = describeUploadError(apiError(413, 'PAYLOAD_TOO_LARGE', 'too big'), {
      max_package_bytes: 26214400,
    })
    expect(described.message).toContain('25 MiB')
  })

  it('explains conflicts, permission errors and network failures', () => {
    const conflict = describeUploadError(
      apiError(409, 'CONFLICT', "Version 1.0.0 of 'x' already exists"),
    )
    expect(conflict.message).toBe("Version 1.0.0 of 'x' already exists")
    expect(conflict.hint).toMatch(/immutable/)

    const forbidden = describeUploadError(apiError(403, 'FORBIDDEN', 'Only the owner can do it'))
    expect(forbidden.hint).toMatch(/owner/)

    const offline = describeUploadError(apiError(0, 'NETWORK_ERROR', 'offline'))
    expect(offline.message).toMatch(/interrupted/)
  })
})

describe('describeError', () => {
  it('handles every documented error code', () => {
    const cases = [
      [apiError(401, 'UNAUTHORIZED', 'x'), /Sign in again/],
      [apiError(403, 'FORBIDDEN', 'You may not'), /You may not/],
      [apiError(404, 'NOT_FOUND', 'nope'), /does not exist/],
      [apiError(409, 'CONFLICT', 'Taken'), /Taken/],
      [apiError(413, 'PAYLOAD_TOO_LARGE', 'x'), /larger than/],
      [apiError(422, 'VALIDATION_ERROR', 'Bad field'), /Bad field/],
      [apiError(500, 'STORAGE_ERROR', 'internal'), /operator/],
      [apiError(500, 'INTERNAL_ERROR', 'x'), /unexpected error/],
      [apiError(0, 'NETWORK_ERROR', 'x'), /could not reach/],
      [apiError(502, 'HTTP_502', 'x'), /unexpected error/],
      [apiError(418, 'HTTP_418', 'teapot'), /teapot/],
    ]
    for (const [error, pattern] of cases) {
      expect(describeError(error).message).toMatch(pattern)
    }
  })

  it('never leaks the details of an unknown JavaScript error', () => {
    const described = describeError(new TypeError('secret internals'))
    expect(described.message).not.toContain('secret')
    expect(described.title).toBe('Something went wrong')
  })
})
