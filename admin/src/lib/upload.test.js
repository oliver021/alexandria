import { describe, expect, it } from 'vitest'
import { MAX_VERSION_LENGTH, validatePackageFile, validateVersion } from './upload.js'

const LIMITS = { max_package_bytes: 26214400 }

describe('validatePackageFile', () => {
  it('accepts a normal zip', () => {
    expect(validatePackageFile({ name: 'pkg.zip', size: 1024 }, LIMITS)).toEqual([])
    expect(validatePackageFile({ name: 'PKG.ZIP', size: 1 }, LIMITS)).toEqual([])
  })

  it('rejects files with another extension', () => {
    const problems = validatePackageFile({ name: 'pkg.tar.gz', size: 1024 }, LIMITS)
    expect(problems).toHaveLength(1)
    expect(problems[0]).toMatch(/not a \.zip file/)
    expect(validatePackageFile({ name: 'zip', size: 5 }, LIMITS)).toHaveLength(1)
    expect(validatePackageFile({ name: 'pkg.zip.exe', size: 5 }, LIMITS)).toHaveLength(1)
  })

  it('rejects empty files', () => {
    expect(validatePackageFile({ name: 'pkg.zip', size: 0 }, LIMITS)).toEqual([
      'The file is empty.',
    ])
  })

  it('rejects files above the limit announced by the registry', () => {
    const problems = validatePackageFile({ name: 'pkg.zip', size: 30 * 1024 * 1024 }, LIMITS)
    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('30 MiB')
    expect(problems[0]).toContain('25 MiB')
  })

  it('accepts a file of exactly the maximum size', () => {
    expect(validatePackageFile({ name: 'pkg.zip', size: 26214400 }, LIMITS)).toEqual([])
  })

  it('skips the size check when the limits are unknown', () => {
    expect(validatePackageFile({ name: 'pkg.zip', size: 10 ** 12 }, null)).toEqual([])
  })

  it('reports every problem at once', () => {
    expect(validatePackageFile({ name: 'pkg.txt', size: 0 }, LIMITS)).toHaveLength(2)
  })
})

describe('validateVersion', () => {
  it('accepts an empty value and valid semantic versions', () => {
    for (const value of ['', '  ', '1.0.0', '0.0.1', '10.20.30', '1.0.0-rc.1', '2.0.0-alpha', ' 1.2.3 ']) {
      expect(validateVersion(value)).toBe('')
    }
  })

  it('rejects anything that is not semver without build metadata', () => {
    for (const value of ['1', '1.0', 'v1.0.0', '01.0.0', '1.0.0.0', '1.0.0-', 'latest', '1.0.0 beta']) {
      expect(validateVersion(value)).not.toBe('')
    }
  })

  it('explains the build metadata restriction', () => {
    expect(validateVersion('1.0.0+build.5')).toMatch(/Build metadata/)
  })

  it('limits the length', () => {
    expect(validateVersion(`1.0.0-${'a'.repeat(MAX_VERSION_LENGTH)}`)).toMatch(/at most/)
  })
})
