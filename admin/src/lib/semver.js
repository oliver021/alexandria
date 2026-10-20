const PATTERN =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+[0-9a-zA-Z.-]+)?$/

/**
 * @param {string} version
 * @returns {{ core: number[], pre: string[] } | null}
 */
export function parseSemver(version) {
  const match = PATTERN.exec(version)
  if (!match) return null
  return {
    core: [Number(match[1]), Number(match[2]), Number(match[3])],
    pre: match[4] ? match[4].split('.') : [],
  }
}

/**
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
function compareIdentifiers(a, b) {
  const numericA = /^\d+$/.test(a)
  const numericB = /^\d+$/.test(b)
  if (numericA && numericB) return Math.sign(Number(a) - Number(b))
  if (numericA) return -1
  if (numericB) return 1
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

/**
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
export function compareSemver(a, b) {
  const left = parseSemver(a)
  const right = parseSemver(b)
  if (!left || !right) {
    if (left) return 1
    if (right) return -1
    return a < b ? -1 : a > b ? 1 : 0
  }
  for (let index = 0; index < 3; index += 1) {
    if (left.core[index] !== right.core[index]) return left.core[index] < right.core[index] ? -1 : 1
  }
  if (left.pre.length === 0 && right.pre.length === 0) return 0
  if (left.pre.length === 0) return 1
  if (right.pre.length === 0) return -1
  const shared = Math.min(left.pre.length, right.pre.length)
  for (let index = 0; index < shared; index += 1) {
    const order = compareIdentifiers(left.pre[index], right.pre[index])
    if (order !== 0) return order
  }
  return Math.sign(left.pre.length - right.pre.length)
}

/**
 * @template {{ version: string }} T
 * @param {readonly T[]} versions
 * @returns {T[]}
 */
export function sortVersionsDescending(versions) {
  return [...versions].sort((a, b) => compareSemver(b.version, a.version))
}
