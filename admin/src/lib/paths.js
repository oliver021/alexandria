const CONTROL_OR_BACKSLASH = /[\u0000-\u001f\u007f\\]/

/**
 * @param {unknown} value
 * @returns {string}
 */
export function encodeSegment(value) {
  if (typeof value !== 'string' || value === '') {
    throw new TypeError('A path segment must be a non-empty string')
  }
  if (value === '.' || value === '..') {
    throw new TypeError('A path segment must not be a dot segment')
  }
  if (value.includes('/') || CONTROL_OR_BACKSLASH.test(value)) {
    throw new TypeError('A path segment must not contain slashes or control characters')
  }
  return encodeURIComponent(value)
}

/**
 * @param {unknown} path
 * @returns {string}
 */
export function encodeFilePath(path) {
  if (typeof path !== 'string' || path === '') {
    throw new TypeError('A file path must be a non-empty string')
  }
  if (path.startsWith('/') || CONTROL_OR_BACKSLASH.test(path)) {
    throw new TypeError('A file path must be relative and free of control characters')
  }
  return path
    .split('/')
    .map((segment) => encodeSegment(segment))
    .join('/')
}

/**
 * @param {string} path
 * @returns {string}
 */
export function basename(path) {
  const index = path.lastIndexOf('/')
  return index === -1 ? path : path.slice(index + 1)
}

/**
 * @param {string} name
 * @returns {string}
 */
export function safeFilename(name) {
  const cleaned = name
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '_')
    .replace(/^\.+$/, '_')
    .trim()
  return (cleaned || 'download').slice(0, 200)
}
