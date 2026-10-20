import { describe, expect, it } from 'vitest'
import { basename, encodeFilePath, encodeSegment, safeFilename } from './paths.js'

describe('encodeSegment', () => {
  it('percent-encodes reserved characters', () => {
    expect(encodeSegment('pdf forms')).toBe('pdf%20forms')
    expect(encodeSegment('1.0.0-rc.1')).toBe('1.0.0-rc.1')
    expect(encodeSegment('a?b#c%d')).toBe('a%3Fb%23c%25d')
  })

  it('rejects values that could change the meaning of a path', () => {
    for (const value of ['', '.', '..', 'a/b', 'a\\b', 'a\u0000b', 'line\nbreak']) {
      expect(() => encodeSegment(value)).toThrow(TypeError)
    }
    expect(() => encodeSegment(undefined)).toThrow(TypeError)
    expect(() => encodeSegment(12)).toThrow(TypeError)
  })
})

describe('encodeFilePath', () => {
  it('encodes every segment and keeps the separators', () => {
    expect(encodeFilePath('references/api notes/ñandú.md')).toBe(
      'references/api%20notes/%C3%B1and%C3%BA.md',
    )
    expect(encodeFilePath('SKILL.md')).toBe('SKILL.md')
  })

  it('rejects traversal, absolute and malformed paths', () => {
    for (const value of [
      '',
      '/etc/passwd',
      '../secret',
      'a/../b',
      'a/./b',
      'a//b',
      'a\\b',
      'a/b/',
      'bad\u0000name',
    ]) {
      expect(() => encodeFilePath(value)).toThrow(TypeError)
    }
  })
})

describe('basename and safeFilename', () => {
  it('returns the last path segment', () => {
    expect(basename('a/b/c.txt')).toBe('c.txt')
    expect(basename('c.txt')).toBe('c.txt')
  })

  it('neutralises characters that are unsafe in file names', () => {
    expect(safeFilename('a/b\\c:d*e?.zip')).toBe('a_b_c_d_e_.zip')
    expect(safeFilename('..')).toBe('_')
    expect(safeFilename('   ')).toBe('download')
    expect(safeFilename('x'.repeat(500))).toHaveLength(200)
  })
})
