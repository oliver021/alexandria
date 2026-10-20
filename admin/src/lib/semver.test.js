import { describe, expect, it } from 'vitest'
import { compareSemver, parseSemver, sortVersionsDescending } from './semver.js'

describe('parseSemver', () => {
  it('splits the core and the prerelease identifiers', () => {
    expect(parseSemver('1.2.3')).toEqual({ core: [1, 2, 3], pre: [] })
    expect(parseSemver('1.2.3-rc.1')).toEqual({ core: [1, 2, 3], pre: ['rc', '1'] })
    expect(parseSemver('1.2.3-alpha+build.5')).toEqual({ core: [1, 2, 3], pre: ['alpha'] })
  })

  it('rejects anything that is not semver', () => {
    for (const value of ['1', '1.0', 'v1.0.0', '01.0.0', 'latest', '', '1.0.0-']) {
      expect(parseSemver(value)).toBeNull()
    }
  })
})

describe('compareSemver', () => {
  it('follows the precedence examples of semver.org', () => {
    const ordered = [
      '1.0.0-alpha',
      '1.0.0-alpha.1',
      '1.0.0-alpha.beta',
      '1.0.0-beta',
      '1.0.0-beta.2',
      '1.0.0-beta.11',
      '1.0.0-rc.1',
      '1.0.0',
      '1.0.1',
      '1.1.0',
      '2.0.0',
    ]
    for (let index = 0; index < ordered.length - 1; index += 1) {
      expect(compareSemver(ordered[index], ordered[index + 1])).toBe(-1)
      expect(compareSemver(ordered[index + 1], ordered[index])).toBe(1)
    }
    expect(compareSemver('1.0.0', '1.0.0')).toBe(0)
  })

  it('compares numbers numerically, not as text', () => {
    expect(compareSemver('1.10.0', '1.9.0')).toBe(1)
    expect(compareSemver('10.0.0', '9.0.0')).toBe(1)
  })

  it('ignores build metadata', () => {
    expect(compareSemver('1.0.0+a', '1.0.0+b')).toBe(0)
  })

  it('puts values that are not semver below those that are', () => {
    expect(compareSemver('1.0.0', 'banana')).toBe(1)
    expect(compareSemver('banana', '1.0.0')).toBe(-1)
    expect(compareSemver('apple', 'banana')).toBe(-1)
  })
})

describe('sortVersionsDescending', () => {
  it('sorts newest first without touching the input', () => {
    const input = [{ version: '1.0.0' }, { version: '1.10.0' }, { version: '1.2.0' }, { version: '2.0.0-rc.1' }, { version: '2.0.0' }]
    const sorted = sortVersionsDescending(input)
    expect(sorted.map((item) => item.version)).toEqual(['2.0.0', '2.0.0-rc.1', '1.10.0', '1.2.0', '1.0.0'])
    expect(input[0].version).toBe('1.0.0')
  })
})
