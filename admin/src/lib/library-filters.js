export const PAGE_SIZE = 20
export const KINDS = Object.freeze(['skill', 'grounding'])
export const STATUSES = Object.freeze(['published', 'draft', 'unpublished'])
export const SORTS = Object.freeze(['relevance', 'name', 'updated', 'created'])
const MAX_PAGE = 100_000

/**
 * @typedef {Object} LibraryFilters
 * @property {string} q
 * @property {string} kind
 * @property {string} category
 * @property {string[]} tags
 * @property {string} status
 * @property {boolean} mine
 * @property {string} sort
 * @property {number} page
 */

/**
 * @returns {LibraryFilters}
 */
export function emptyFilters() {
  return { q: '', kind: '', category: '', tags: [], status: '', mine: false, sort: '', page: 1 }
}

/**
 * @param {string | null} value
 * @param {readonly string[]} allowed
 * @returns {string}
 */
function oneOf(value, allowed) {
  return value !== null && allowed.includes(value) ? value : ''
}

/**
 * @param {URLSearchParams} query
 * @returns {LibraryFilters}
 */
export function filtersFromQuery(query) {
  const page = Number.parseInt(query.get('page') ?? '1', 10)
  /** @type {string[]} */
  const tags = []
  for (const tag of query.getAll('tag')) {
    const clean = tag.trim()
    if (clean && !tags.includes(clean)) tags.push(clean)
  }
  return {
    q: (query.get('q') ?? '').trim(),
    kind: oneOf(query.get('kind'), KINDS),
    category: (query.get('category') ?? '').trim(),
    tags,
    status: oneOf(query.get('status'), STATUSES),
    mine: ['1', 'true'].includes(query.get('mine') ?? ''),
    sort: oneOf(query.get('sort'), SORTS),
    page: Number.isInteger(page) && page >= 1 ? Math.min(page, MAX_PAGE) : 1,
  }
}

/**
 * @param {LibraryFilters} filters
 * @returns {Record<string, string | string[] | boolean>}
 */
export function queryFromFilters(filters) {
  /** @type {Record<string, string | string[] | boolean>} */
  const query = {}
  if (filters.q) query.q = filters.q
  if (filters.kind) query.kind = filters.kind
  if (filters.category) query.category = filters.category
  if (filters.tags.length > 0) query.tag = filters.tags
  if (filters.status) query.status = filters.status
  if (filters.mine) query.mine = true
  if (filters.sort) query.sort = filters.sort
  if (filters.page > 1) query.page = String(filters.page)
  return query
}

/**
 * @param {LibraryFilters} filters
 * @param {number} [pageSize]
 * @returns {Record<string, string | string[] | boolean | number>}
 */
export function apiParamsFromFilters(filters, pageSize = PAGE_SIZE) {
  /** @type {Record<string, string | string[] | boolean | number>} */
  const params = { limit: pageSize, offset: (filters.page - 1) * pageSize }
  if (filters.q) params.q = filters.q
  if (filters.kind) params.kind = filters.kind
  if (filters.category) params.category = filters.category
  if (filters.tags.length > 0) params.tag = filters.tags
  if (filters.status) params.status = filters.status
  if (filters.mine) params.mine = true
  if (filters.sort) params.sort = filters.sort
  return params
}

/**
 * @param {LibraryFilters} filters
 * @returns {number}
 */
export function activeFilterCount(filters) {
  return (
    (filters.q ? 1 : 0) +
    (filters.kind ? 1 : 0) +
    (filters.category ? 1 : 0) +
    filters.tags.length +
    (filters.status ? 1 : 0) +
    (filters.mine ? 1 : 0)
  )
}

/**
 * @param {number} total
 * @param {number} [pageSize]
 * @returns {number}
 */
export function pageCountFor(total, pageSize = PAGE_SIZE) {
  return Math.max(1, Math.ceil(total / pageSize))
}

/**
 * @param {number} page
 * @param {number} count
 * @param {number} [radius]
 * @returns {Array<number | 'gap'>}
 */
export function pageWindow(page, count, radius = 1) {
  /** @type {Array<number | 'gap'>} */
  const items = []
  /** @type {number | null} */
  let previous = null
  for (let current = 1; current <= count; current += 1) {
    const visible = current === 1 || current === count || Math.abs(current - page) <= radius
    if (!visible) continue
    if (previous !== null && current - previous > 1) {
      items.push(current - previous === 2 ? previous + 1 : 'gap')
    }
    items.push(current)
    previous = current
  }
  return items
}
