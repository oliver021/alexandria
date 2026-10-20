import { formatBytes } from './format.js'

export const PACKAGE_REASONS = Object.freeze({
  NOT_A_ZIP: {
    title: 'This is not a zip archive',
    explanation:
      'The file could not be read as a zip archive. A renamed .tar.gz or a downloaded error page are common causes.',
    fix: 'Create the archive with "alx pack" or any zip tool and upload that .zip file.',
  },
  CORRUPT_ARCHIVE: {
    title: 'The archive is damaged or inconsistent',
    explanation:
      'The zip is truncated, has extra bytes, hides data between its entries or has an inconsistent central directory.',
    fix: 'Build the package again with "alx pack" and upload the new file.',
  },
  ENCRYPTED_ENTRY: {
    title: 'The archive is password protected',
    explanation: 'At least one entry of the zip is encrypted, and the registry cannot read it.',
    fix: 'Compress the package again without a password.',
  },
  UNSUPPORTED_COMPRESSION: {
    title: 'Unsupported compression method',
    explanation: 'Only the deflate and stored compression methods are accepted. bzip2, lzma and zstd are not.',
    fix: 'Zip the package with the default deflate method.',
  },
  SYMLINK_NOT_ALLOWED: {
    title: 'The archive contains a symbolic link',
    explanation: 'Symbolic links can point outside the folder where a consumer extracts the package.',
    fix: 'Replace each link with a copy of the file it points to.',
  },
  UNSAFE_PATH: {
    title: 'A file path in the archive is not safe',
    explanation:
      'A path is absolute, contains "..", a backslash or control characters, or is longer than 255 characters.',
    fix: 'Rename the offending file so that it uses a short relative path.',
  },
  DUPLICATE_PATH: {
    title: 'Two files share the same path',
    explanation:
      'Two paths are equal after normalization (case or Unicode form), or a name is used both as a file and as a folder.',
    fix: 'Keep a single file for each path.',
  },
  TOO_MANY_FILES: {
    title: 'The package has too many files',
    explanation: 'The archive holds more files than the registry accepts. A folder such as node_modules is a typical cause.',
    fix: 'Exclude dependencies and generated files, or bundle the data into fewer files.',
  },
  TOO_LARGE: {
    title: 'The package is too large',
    explanation:
      'The archive, a single file or the total uncompressed size exceeds a limit of this registry.',
    fix: 'Reduce the package, compress the data, or store large files elsewhere and link to them.',
  },
  MISSING_ENTRYPOINT: {
    title: 'The entry point is missing',
    explanation:
      'The root of the package must contain SKILL.md (for a skill) or INDEX.md (for a grounding package), spelled exactly like that.',
    fix: 'Add the entry point at the root of the archive, or inside the single wrapper folder.',
  },
  AMBIGUOUS_ENTRYPOINT: {
    title: 'The package has two entry points',
    explanation: 'The root contains both SKILL.md and INDEX.md, so the registry cannot tell which kind of entry this is.',
    fix: 'Keep only the entry point that matches the kind of the package.',
  },
  BAD_FRONTMATTER: {
    title: 'The entry point front matter cannot be read',
    explanation:
      'The YAML block at the top of the entry point is missing, not closed by a "---" line, invalid, too big, or uses anchors, aliases or repeated keys.',
    fix: 'Fix the block between the "---" lines and check it with "alx validate".',
  },
  MISSING_FIELD: {
    title: 'A required field is missing',
    explanation: 'The front matter needs a name and a description. The version must be in the front matter or entered in the form.',
    fix: 'Add the missing field, or type the version in the form below.',
  },
  INVALID_FIELD: {
    title: 'A field has an invalid value',
    explanation:
      'A name, description, version, tag or category does not follow the rules. A version such as 1.0 must be written as a quoted semantic version like "1.0.0".',
    fix: 'Adjust the value to the format that the server message describes.',
  },
  KIND_MISMATCH: {
    title: 'The declared kind does not match the entry point',
    explanation: 'The "kind" field says one thing while the entry point file (SKILL.md or INDEX.md) says another.',
    fix: 'Remove the "kind" field or make it match the entry point.',
  },
  VERSION_MISMATCH: {
    title: 'The version differs from the one in the package',
    explanation: 'The version typed in the form is not the same as the version in the front matter.',
    fix: 'Use the same version in both places, or leave the form field empty.',
  },
})

/**
 * @typedef {Object} ErrorDescription
 * @property {string} title
 * @property {string} message
 * @property {string} [hint]
 * @property {string} [reason]
 * @property {string} [code]
 * @property {string} [serverMessage]
 */

const NETWORK = {
  title: 'The registry is unreachable',
  message: 'The request could not reach the registry. Check your connection and try again.',
}

/**
 * @param {unknown} error
 * @returns {{ status: number, code: string, message: string, reason: string | null } | null}
 */
function asApiError(error) {
  if (
    error &&
    typeof error === 'object' &&
    'status' in error &&
    'code' in error &&
    typeof error.code === 'string' &&
    typeof error.status === 'number'
  ) {
    return /** @type {any} */ (error)
  }
  return null
}

/**
 * @param {unknown} error
 * @returns {ErrorDescription}
 */
export function describeError(error) {
  const api = asApiError(error)
  if (!api) {
    return {
      title: 'Something went wrong',
      message: 'An unexpected error occurred. Reload the page and try again.',
    }
  }
  const base = { code: api.code, reason: api.reason ?? undefined, serverMessage: api.message }
  switch (api.code) {
    case 'NETWORK_ERROR':
      return { ...NETWORK, ...base }
    case 'UNAUTHORIZED':
      return {
        ...base,
        title: 'Authentication required',
        message: 'Your session has expired or the credential is not valid. Sign in again.',
      }
    case 'FORBIDDEN':
      return { ...base, title: 'Not allowed', message: api.message }
    case 'NOT_FOUND':
      return {
        ...base,
        title: 'Not found',
        message: 'It does not exist, or you are not allowed to see it.',
      }
    case 'CONFLICT':
      return { ...base, title: 'Conflict', message: api.message }
    case 'PAYLOAD_TOO_LARGE':
      return {
        ...base,
        title: 'The upload is too large',
        message: 'The file is larger than this registry accepts.',
      }
    case 'VALIDATION_ERROR':
      return { ...base, title: 'Invalid request', message: api.message }
    case 'INVALID_PACKAGE': {
      const known = api.reason ? PACKAGE_REASONS[/** @type {keyof typeof PACKAGE_REASONS} */ (api.reason)] : undefined
      if (known) return { ...base, title: known.title, message: known.explanation, hint: known.fix }
      return { ...base, title: 'The package is not valid', message: api.message }
    }
    case 'STORAGE_ERROR':
      return {
        ...base,
        title: 'The registry could not read the stored package',
        message: 'Tell the registry operator: the stored data of this version is missing or damaged.',
      }
    default:
      if (api.status >= 500) {
        return {
          ...base,
          title: 'The registry failed',
          message: 'The registry hit an unexpected error. Try again; if it persists, tell the operator.',
        }
      }
      return { ...base, title: 'The request failed', message: api.message }
  }
}

/**
 * @param {unknown} error
 * @param {import('./types.js').Limits | null} [limits]
 * @returns {ErrorDescription}
 */
export function describeUploadError(error, limits = null) {
  const api = asApiError(error)
  if (!api) return describeError(error)
  const described = describeError(error)
  switch (api.code) {
    case 'PAYLOAD_TOO_LARGE':
      return {
        ...described,
        message: limits
          ? `The file is larger than the registry accepts (${formatBytes(limits.max_package_bytes)}).`
          : described.message,
        hint: 'Reduce the package or store large files elsewhere and link to them.',
      }
    case 'CONFLICT':
      return {
        ...described,
        title: 'The entry rejected this upload',
        message: api.message,
        hint: 'Versions are immutable and the kind of an entry cannot change. To change a package, upload it again with a new version.',
      }
    case 'FORBIDDEN':
      return {
        ...described,
        title: 'You cannot upload this package',
        message: api.message,
        hint: 'Only the owner of an entry or an admin can add versions to it.',
      }
    case 'VALIDATION_ERROR':
      return {
        ...described,
        hint: 'Check the version field and the package contents.',
      }
    case 'NETWORK_ERROR':
      return {
        ...described,
        message: 'The upload was interrupted before the registry answered. Check your connection and try again.',
      }
    default:
      return described
  }
}
