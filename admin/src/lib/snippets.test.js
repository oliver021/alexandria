import { describe, expect, it } from 'vitest'
import { TOKEN_PLACEHOLDER, buildSnippets, joinUrl, shellQuote } from './snippets.js'

const INPUT = {
  publicUrl: 'https://registry.example.test/',
  mcpPath: '/mcp',
  name: 'pdf-forms',
  version: '1.2.0',
  downloadUrl: 'https://registry.example.test/api/v1/skills/pdf-forms/versions/1.2.0/download',
}

describe('joinUrl', () => {
  it('joins without duplicating slashes', () => {
    expect(joinUrl('https://x.test/', '/mcp')).toBe('https://x.test/mcp')
    expect(joinUrl('https://x.test', 'mcp')).toBe('https://x.test/mcp')
    expect(joinUrl('https://x.test//', '//mcp')).toBe('https://x.test/mcp')
  })
})

describe('shellQuote', () => {
  it('leaves safe words alone and quotes the rest', () => {
    expect(shellQuote('pdf-forms@1.2.0')).toBe('pdf-forms@1.2.0')
    expect(shellQuote('a b')).toBe("'a b'")
    expect(shellQuote("it's")).toBe("'it'\\''s'")
    expect(shellQuote('$(rm -rf /)')).toBe("'$(rm -rf /)'")
  })
})

describe('buildSnippets', () => {
  const snippets = buildSnippets(INPUT)

  it('builds the MCP URL from the registry information', () => {
    expect(snippets.mcpUrl).toBe('https://registry.example.test/mcp')
  })

  it('produces a valid MCP configuration with an Authorization header', () => {
    const config = JSON.parse(snippets.mcpJson)
    expect(config.mcpServers.alexandria).toEqual({
      type: 'http',
      url: 'https://registry.example.test/mcp',
      headers: { Authorization: `Bearer ${TOKEN_PLACEHOLDER}` },
    })
  })

  it('produces the claude mcp add command', () => {
    expect(snippets.claudeAdd).toBe(
      'claude mcp add --transport http alexandria https://registry.example.test/mcp ' +
        `--header "Authorization: Bearer ${TOKEN_PLACEHOLDER}"`,
    )
  })

  it('produces a curl download that keeps the token out of the URL', () => {
    expect(snippets.curl).toContain('-H "Authorization: Bearer $ALX_TOKEN"')
    expect(snippets.curl).toContain('-o pdf-forms-1.2.0.zip')
    expect(snippets.curl).toContain(INPUT.downloadUrl)
    expect(snippets.curl).not.toMatch(/[?&]token=/)
  })

  it('produces the alx pull command and its environment', () => {
    expect(snippets.alx).toBe('alx pull pdf-forms@1.2.0')
    expect(snippets.alxEnv).toBe(
      `export ALX_URL=https://registry.example.test/\nexport ALX_TOKEN='${TOKEN_PLACEHOLDER}'`,
    )
  })

  it('never contains anything that looks like a real credential', () => {
    for (const text of Object.values(snippets)) {
      expect(text).not.toMatch(/alx_[A-Za-z0-9_-]{20,}/)
      expect(text).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/)
    }
  })

  it('quotes values that would be dangerous in a shell', () => {
    const risky = buildSnippets({ ...INPUT, name: 'x; rm -rf ~', version: '1.0.0' })
    expect(risky.alx).toBe("alx pull 'x; rm -rf ~@1.0.0'")
    expect(risky.curl).toContain("-o 'x; rm -rf ~-1.0.0.zip'")
  })
})
