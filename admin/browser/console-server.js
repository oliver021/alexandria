// A server for the browser tests: it hosts a build of the console under /admin/ the way the registry
// has to (see ../hosting.js) and lets a test answer the calls of the console to /api/v1.

import { execFile } from 'node:child_process'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, normalize, sep } from 'node:path'
import { promisify } from 'node:util'
import { CONSOLE_PATH, expectedHeaders } from '../hosting.js'

const run = promisify(execFile)
const ADMIN_ROOT = join(import.meta.dirname, '..')

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
}

/**
 * The environment of `npm run build`. The test runner sets variables (`VITEST`, `NODE_ENV=test`) that
 * make the Svelte plugins build for tests, and a console built that way does not start in a browser.
 *
 * @returns {NodeJS.ProcessEnv}
 */
function buildEnvironment() {
  const env = { ...process.env, NODE_ENV: 'production' }
  for (const name of Object.keys(env)) {
    if (name.startsWith('VITEST') || name === 'TEST' || name === 'MODE') delete env[name]
  }
  return env
}

/**
 * Builds the console exactly like `npm run build`, in a folder of its own. It runs as a process, so
 * that the build does not see the environment of the test runner.
 *
 * @param {string} outDir
 */
export async function buildConsole(outDir) {
  const vite = join(ADMIN_ROOT, 'node_modules', 'vite', 'bin', 'vite.js')
  await run(process.execPath, [vite, 'build', '--outDir', outDir, '--emptyOutDir', '--logLevel', 'error'], {
    cwd: ADMIN_ROOT,
    env: buildEnvironment(),
  })
}

/**
 * @param {import('node:http').ServerResponse} response
 * @param {number} status
 * @param {unknown} body
 * @param {Record<string, string>} [headers]
 */
export function sendJson(response, status, body, headers = {}) {
  response.writeHead(status, { 'content-type': 'application/json', ...headers })
  response.end(JSON.stringify(body))
}

/**
 * @typedef {(request: import('node:http').IncomingMessage, response: import('node:http').ServerResponse, url: URL) => boolean | Promise<boolean>} ApiHandler
 *   answers a request and returns true, or returns false to say that it is not its request
 */

/**
 * @param {{ root: string, api?: ApiHandler, hosting?: boolean, pages?: Record<string, string> }} options
 *   `root` is a folder with a build of the console. With `hosting: false` the server sends nothing but
 *   the file, like a plain static server, so that a test can show what the checks catch. `pages` are
 *   HTML documents served outside the console, for example a page that frames it.
 * @returns {Promise<{ origin: string, requests: string[], close: () => Promise<void> }>}
 */
export async function serveConsole({ root, api = () => false, hosting = true, pages = {} }) {
  /** @type {string[]} every request that reached the server, as "METHOD /path" */
  const requests = []

  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://localhost')
    const path = url.pathname
    requests.push(`${request.method} ${path}`)

    /**
     * @param {number} status
     * @param {Record<string, string>} headers
     */
    const head = (status, headers) => {
      response.writeHead(status, { ...(hosting ? expectedHeaders(path, status) : {}), ...headers })
    }

    if (path === CONSOLE_PATH) {
      head(307, { location: `${CONSOLE_PATH}/` })
      response.end()
      return
    }

    if (path.startsWith(`${CONSOLE_PATH}/`)) {
      let relative = normalize(path.slice(CONSOLE_PATH.length + 1))
      if (relative === '.' || relative.endsWith(sep)) relative = join(relative, 'index.html')
      const file = join(root, relative)
      if (!file.startsWith(root + sep) || !existsSync(file) || !statSync(file).isFile()) {
        head(404, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'Not Found' } }))
        return
      }
      const stats = statSync(file)
      const etag = `"${stats.size.toString(16)}-${Math.round(stats.mtimeMs).toString(16)}"`
      if (request.headers['if-none-match'] === etag) {
        head(304, { etag })
        response.end()
        return
      }
      head(200, {
        'content-type': TYPES[extname(file)] ?? 'application/octet-stream',
        'content-length': String(stats.size),
        etag,
      })
      createReadStream(file).pipe(response)
      return
    }

    if (Object.hasOwn(pages, path)) {
      response.writeHead(200, { 'content-type': TYPES['.html'] })
      response.end(pages[path])
      return
    }

    if (path === '/api/v1/health') {
      sendJson(response, 200, { status: 'ok' })
      return
    }

    if (await api(request, response, url)) return
    sendJson(response, 404, { error: { code: 'NOT_FOUND', message: `The test server has no answer for ${path}` } })
  })

  await new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(undefined)))
  const address = /** @type {import('node:net').AddressInfo} */ (server.address())
  return {
    origin: `http://127.0.0.1:${address.port}`,
    requests,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve(undefined))
      }),
  }
}
