import { formatBytes } from './format.js'

export const SEMVER =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?$/

export const MAX_VERSION_LENGTH = 50

/**
 * @param {string} value
 * @returns {string}
 */
export function validateVersion(value) {
  const text = value.trim()
  if (text === '') return ''
  if (text.length > MAX_VERSION_LENGTH) {
    return `A version has at most ${MAX_VERSION_LENGTH} characters.`
  }
  if (text.includes('+')) {
    return 'Build metadata (+...) is not supported. Use MAJOR.MINOR.PATCH with an optional -prerelease.'
  }
  if (!SEMVER.test(text)) {
    return 'Use a semantic version such as 1.2.0 or 2.0.0-rc.1.'
  }
  return ''
}

/**
 * @param {{ name: string, size: number }} file
 * @param {{ max_package_bytes: number } | null | undefined} limits
 * @returns {string[]}
 */
export function validatePackageFile(file, limits) {
  const problems = []
  if (!/\.zip$/i.test(file.name)) {
    problems.push(`"${file.name}" is not a .zip file. Packages are uploaded as .zip archives.`)
  }
  if (file.size === 0) {
    problems.push('The file is empty.')
  } else if (limits && file.size > limits.max_package_bytes) {
    problems.push(
      `The file is ${formatBytes(file.size)}, larger than the ${formatBytes(limits.max_package_bytes)} this registry accepts.`,
    )
  }
  return problems
}
