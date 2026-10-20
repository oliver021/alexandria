import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'
import { CONTENT_SECURITY_POLICY } from '../csp.js'

const ROOT = join(import.meta.dirname, '..')
const SRC = join(ROOT, 'src')

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const sources = walk(SRC)
  .filter((path) => /\.(js|svelte|css)$/.test(path))
  .filter((path) => !/\.test\.js$/.test(path) && !path.includes(`${join(SRC, 'test')}`))
  .map((path) => ({ file: relative(ROOT, path), text: readFileSync(path, 'utf8') }))

function findings(pattern) {
  return sources
    .filter(({ text }) => pattern.test(text))
    .map(({ file }) => file)
}

describe('source code security rules', () => {
  it('scans the real source tree', () => {
    expect(sources.length).toBeGreaterThan(40)
    expect(sources.some(({ file }) => file.endsWith('api.js'))).toBe(true)
  })

  it('never injects HTML', () => {
    expect(findings(/\{@html/)).toEqual([])
    expect(findings(/\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write/)).toEqual([])
    expect(findings(/dangerouslySetInnerHTML/)).toEqual([])
    expect(findings(/srcdoc/)).toEqual([])
  })

  it('never evaluates strings as code', () => {
    expect(findings(/\beval\s*\(/)).toEqual([])
    expect(findings(/new\s+Function\s*\(/)).toEqual([])
    expect(findings(/set(Timeout|Interval)\s*\(\s*['"`]/)).toEqual([])
  })

  it('never builds javascript: or data: links', () => {
    expect(findings(/javascript:/i)).toEqual([])
    expect(findings(/href=\{?["'`]?data:/i)).toEqual([])
  })

  it('uses style directives, never inline style attributes, so the strict CSP keeps working', () => {
    const svelte = sources.filter(({ file }) => file.endsWith('.svelte'))
    for (const { file, text } of svelte) {
      expect(text, file).not.toMatch(/\sstyle=["'{]/)
    }
  })

  it('keeps the session out of localStorage', () => {
    expect(findings(/localStorage/)).toEqual([])
  })

  it('does not load anything from outside the origin', () => {
    expect(findings(/https?:\/\/(?!localhost|registry\.example|www\.w3\.org)/)).toEqual([])
    expect(findings(/@import\s+url\(/)).toEqual([])
    expect(findings(/<script[^>]+src=/)).toEqual([])
  })

  it('uses Svelte 5 runes only', () => {
    const svelte = sources.filter(({ file }) => file.endsWith('.svelte'))
    expect(svelte.length).toBeGreaterThan(20)
    for (const { file, text } of svelte) {
      expect(text, file).not.toMatch(/\bexport\s+let\b/)
      expect(text, file).not.toMatch(/\son:[a-z]+/)
      expect(text, file).not.toMatch(/<slot[\s>]/)
      expect(text, file).not.toMatch(/\$:\s/)
      expect(text, file).not.toMatch(/from\s+['"]svelte\/store['"]/)
      expect(text, file).not.toMatch(/createEventDispatcher/)
    }
  })

  it('does not put credentials in URLs', () => {
    const api = sources.find(({ file }) => file.endsWith(join('lib', 'api.js'))).text
    expect(api).not.toMatch(/[?&](token|access_token|auth)=/)
    expect(api).toMatch(/Authorization/)
  })

  it('encodes every dynamic piece of an API path', () => {
    const api = sources.find(({ file }) => file.endsWith(join('lib', 'api.js'))).text
    const dynamic = [...api.matchAll(/`([^`]*\$\{[^`]*)`/g)].map((match) => match[1])
    for (const template of dynamic) {
      const parts = [...template.matchAll(/\$\{([^}]+)\}/g)].map((match) => match[1])
      for (const part of parts) {
        expect(part, template).toMatch(
          /^(API_BASE|path|queryString\([^)]*\)|entryPath\(ref\)|versionPath\(ref, version\)|encodeSegment\([^)]*\)|encodeFilePath\([^)]*\)|status\b.*|statusText\b.*|token\b.*)$/,
        )
      }
    }
  })
})

describe('index.html', () => {
  const html = readFileSync(join(ROOT, 'index.html'), 'utf8')

  it('has its own title, favicon and language', () => {
    expect(html).toContain('<html lang="en">')
    expect(html).toMatch(/<title>Alexandria Admin<\/title>/)
    expect(html).toContain('href="/favicon.svg"')
  })

  it('does not load anything from another origin', () => {
    expect(html).not.toMatch(/https?:\/\/(?!www\.w3\.org)/)
  })

  it('gets a strict Content Security Policy at build time', () => {
    const config = readFileSync(join(ROOT, 'vite.config.js'), 'utf8')
    expect(config).toContain('CONTENT_SECURITY_POLICY')
    expect(config).toContain("'http-equiv': 'Content-Security-Policy'")
    const directives = Object.fromEntries(
      CONTENT_SECURITY_POLICY.split('; ').map((part) => {
        const [name, ...values] = part.split(' ')
        return [name, values]
      }),
    )
    expect(directives['default-src']).toEqual(["'self'"])
    expect(directives['script-src']).toEqual(["'self'"])
    expect(directives['style-src']).toEqual(["'self'"])
    expect(directives['connect-src']).toEqual(["'self'"])
    expect(directives['object-src']).toEqual(["'none'"])
    expect(directives['base-uri']).toEqual(["'self'"])
    expect(CONTENT_SECURITY_POLICY).not.toContain('unsafe-inline')
    expect(CONTENT_SECURITY_POLICY).not.toContain('unsafe-eval')
    expect(CONTENT_SECURITY_POLICY).not.toMatch(/https?:|\*/)
  })

  it('only holds directives that a meta tag honours, so framing is left to the HTTP header', () => {
    const names = CONTENT_SECURITY_POLICY.split('; ').map((part) => part.split(' ')[0])
    for (const ignoredInMeta of ['frame-ancestors', 'report-uri', 'report-to', 'sandbox']) {
      expect(names).not.toContain(ignoredInMeta)
    }
    expect(new Set(names).size).toBe(names.length)
  })

  it('declares both color schemes', () => {
    expect(html).toContain('name="color-scheme" content="light dark"')
  })
})

describe('obsolete template files', () => {
  it('are gone', () => {
    const files = walk(ROOT)
      .filter((path) => !path.includes('node_modules') && !path.includes(`${join(ROOT, 'dist')}`))
      .map((path) => relative(ROOT, path))
    for (const stale of ['Signup.svelte', 'Counter.svelte', 'hero.png', 'svelte.svg', 'vite.svg', 'icons.svg']) {
      expect(files.filter((file) => file.endsWith(stale))).toEqual([])
    }
  })
})
