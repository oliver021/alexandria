export const TOKEN_PLACEHOLDER = '<YOUR_API_TOKEN>'

/**
 * @param {string} value
 * @returns {string}
 */
export function shellQuote(value) {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value
  return `'${value.replace(/'/g, `'\\''`)}'`
}

/**
 * @param {string} base
 * @param {string} path
 * @returns {string}
 */
export function joinUrl(base, path) {
  return `${base.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`
}

/**
 * @typedef {Object} SnippetInput
 * @property {string} publicUrl
 * @property {string} mcpPath
 * @property {string} name
 * @property {string} version
 * @property {string} downloadUrl
 */

/**
 * @param {SnippetInput} input
 * @returns {{ mcpUrl: string, mcpJson: string, claudeAdd: string, curl: string, alx: string, alxEnv: string }}
 */
export function buildSnippets({ publicUrl, mcpPath, name, version, downloadUrl }) {
  const mcpUrl = joinUrl(publicUrl, mcpPath)
  const mcpJson = JSON.stringify(
    {
      mcpServers: {
        alexandria: {
          type: 'http',
          url: mcpUrl,
          headers: { Authorization: `Bearer ${TOKEN_PLACEHOLDER}` },
        },
      },
    },
    null,
    2,
  )
  const claudeAdd = [
    'claude mcp add --transport http alexandria',
    shellQuote(mcpUrl),
    `--header "Authorization: Bearer ${TOKEN_PLACEHOLDER}"`,
  ].join(' ')
  const curl = [
    'curl -fSL',
    '-H "Authorization: Bearer $ALX_TOKEN"',
    `-o ${shellQuote(`${name}-${version}.zip`)}`,
    shellQuote(downloadUrl),
  ].join(' ')
  const alxEnv = [
    `export ALX_URL=${shellQuote(publicUrl)}`,
    `export ALX_TOKEN=${shellQuote(TOKEN_PLACEHOLDER)}`,
  ].join('\n')
  const alx = `alx pull ${shellQuote(`${name}@${version}`)}`
  return { mcpUrl, mcpJson, claudeAdd, curl, alx, alxEnv }
}
