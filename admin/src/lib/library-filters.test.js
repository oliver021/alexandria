import { describe, expect, it } from 'vitest'
import {
  PAGE_SIZE,
  activeFilterCount,
  apiParamsFromFilters,
  emptyFilters,
  filtersFromQuery,
  pageCountFor,
  pageWindow,
  queryFromFilters,
} from './library-filters.js'
import { queryString } from './routes.js'

describe('filtersFromQuery', () => {
  it('reads every filter from the address', () => {
    const filters = filtersFromQuery(
      new URLSearchParams(
        'q=%20pdf%20&kind=skill&category=docs&tag=a&tag=b&status=draft&mine=1&sort=updated&page=3',
      ),
    )
    expect(filters).toEqual({
      q: 'pdf',
      kind: 'skill',
      category: 'docs',
      tags: ['a', 'b'],
      status: 'draft',
      mine: true,
      sort: 'updated',
      page: 3,
    })
  })

  it('ignores invalid enumerated values and malformed numbers', () => {
    const filters = filtersFromQuery(
      new URLSearchParams('kind=wizard&status=gone&sort=random&page=-4&mine=maybe'),
    )
    expect(filters).toEqual(emptyFilters())
    expect(filtersFromQuery(new URLSearchParams('page=abc')).page).toBe(1)
    expect(filtersFromQuery(new URLSearchParams('page=2.5')).page).toBe(2)
    expect(filtersFromQuery(new URLSearchParams('page=99999999999')).page).toBeLessThanOrEqual(
      100000,
    )
  })

  it('deduplicates and trims tags', () => {
    expect(filtersFromQuery(new URLSearchParams('tag=a&tag=%20a%20&tag=&tag=b')).tags).toEqual([
      'a',
      'b',
    ])
  })
})

describe('queryFromFilters', () => {
  it('omits defaults', () => {
    expect(queryFromFilters(emptyFilters())).toEqual({})
  })

  it('round-trips through the address', () => {
    const filters = {
      q: 'api design',
      kind: 'grounding',
      category: 'api-design',
      tags: ['http', 'rest'],
      status: 'published',
      mine: true,
      sort: 'created',
      page: 4,
    }
    const text = queryString(queryFromFilters(filters))
    expect(filtersFromQuery(new URLSearchParams(text.slice(1)))).toEqual(filters)
  })
})

describe('apiParamsFromFilters', () => {
  it('maps filters to API parameters with the page offset', () => {
    const params = apiParamsFromFilters({
      ...emptyFilters(),
      q: 'pdf',
      kind: 'skill',
      tags: ['a'],
      mine: true,
      page: 3,
    })
    expect(params).toEqual({
      limit: PAGE_SIZE,
      offset: 2 * PAGE_SIZE,
      q: 'pdf',
      kind: 'skill',
      tag: ['a'],
      mine: true,
    })
  })

  it('asks for the first page by default', () => {
    expect(apiParamsFromFilters(emptyFilters())).toEqual({ limit: PAGE_SIZE, offset: 0 })
  })
})

describe('counting and paging', () => {
  it('counts active filters', () => {
    expect(activeFilterCount(emptyFilters())).toBe(0)
    expect(
      activeFilterCount({ ...emptyFilters(), q: 'x', tags: ['a', 'b'], mine: true, kind: 'skill' }),
    ).toBe(5)
    expect(activeFilterCount({ ...emptyFilters(), sort: 'name', page: 3 })).toBe(0)
  })

  it('computes the number of pages', () => {
    expect(pageCountFor(0)).toBe(1)
    expect(pageCountFor(20)).toBe(1)
    expect(pageCountFor(21)).toBe(2)
    expect(pageCountFor(133)).toBe(7)
  })

  it('builds a compact pagination window', () => {
    expect(pageWindow(1, 1)).toEqual([1])
    expect(pageWindow(1, 3)).toEqual([1, 2, 3])
    expect(pageWindow(1, 10)).toEqual([1, 2, 'gap', 10])
    expect(pageWindow(5, 10)).toEqual([1, 'gap', 4, 5, 6, 'gap', 10])
    expect(pageWindow(10, 10)).toEqual([1, 'gap', 9, 10])
    expect(pageWindow(3, 6)).toEqual([1, 2, 3, 4, 5, 6])
    expect(pageWindow(2, 8)).toEqual([1, 2, 3, 'gap', 8])
    expect(pageWindow(4, 7)).toEqual([1, 2, 3, 4, 5, 6, 7])
  })
})
