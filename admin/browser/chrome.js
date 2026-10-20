// A small driver for a real Chrome, used by the browser tests. It speaks the Chrome DevTools Protocol
// over the pipe that `--remote-debugging-pipe` opens, so it needs no package and no network port, and
// Chrome quits by itself if the test process dies.
//
// Layout and scrolling cannot be tested in jsdom, which draws nothing. Everything here is Linux and
// macOS only, and `findChrome()` returns null when no Chrome is installed so that the tests can skip.

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const LAUNCH_TIMEOUT_MS = 20_000
const EXIT_TIMEOUT_MS = 3_000

const KNOWN_LOCATIONS = [
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/snap/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
]

/**
 * @param {NodeJS.ProcessEnv} [env] `CHROME_BIN` or `CHROME_PATH` name the executable to use
 * @returns {string | null}
 */
export function findChrome(env = process.env) {
  const candidates = [env.CHROME_BIN, env.CHROME_PATH, ...KNOWN_LOCATIONS]
  return candidates.find((path) => path && existsSync(path)) ?? null
}

/**
 * The two ends of the pipe, with the framing of the protocol: JSON messages that end with a NUL byte.
 */
class Connection {
  /**
   * @param {import('node:stream').Readable} input what Chrome writes
   * @param {import('node:stream').Writable} output what Chrome reads
   */
  constructor(input, output) {
    this.output = output
    // Writing to Chrome after it quit is an error that nobody can act on.
    output.on('error', () => undefined)
    this.nextId = 1
    /** @type {Map<number, { resolve: (value: any) => void, reject: (error: Error) => void, method: string }>} */
    this.pending = new Map()
    /** @type {Set<(message: any) => void>} */
    this.listeners = new Set()
    let buffered = ''
    input.setEncoding('utf8')
    input.on('data', (chunk) => {
      buffered += chunk
      let end = buffered.indexOf('\0')
      while (end !== -1) {
        this.dispatch(JSON.parse(buffered.slice(0, end)))
        buffered = buffered.slice(end + 1)
        end = buffered.indexOf('\0')
      }
    })
  }

  /** @param {any} message */
  dispatch(message) {
    if (message.id !== undefined) {
      const waiting = this.pending.get(message.id)
      if (!waiting) return
      this.pending.delete(message.id)
      if (message.error) waiting.reject(new Error(`${waiting.method}: ${message.error.message}`))
      else waiting.resolve(message.result)
      return
    }
    for (const listener of this.listeners) listener(message)
  }

  /**
   * @param {string} method
   * @param {Record<string, unknown>} [params]
   * @param {string} [sessionId]
   * @returns {Promise<any>}
   */
  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method })
      this.output.write(`${JSON.stringify({ id, method, params, sessionId })}\0`)
    })
  }

  /**
   * @param {(message: any) => void} listener
   * @returns {() => void} stops listening
   */
  listen(listener) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  fail(reason) {
    for (const { reject, method } of this.pending.values()) reject(new Error(`${method}: ${reason}`))
    this.pending.clear()
  }
}

/**
 * One tab. Everything the page reports about itself while a test runs is kept in `problems`.
 */
export class Page {
  /**
   * @param {Connection} connection
   * @param {string} sessionId
   * @param {string} targetId
   */
  constructor(connection, sessionId, targetId) {
    this.connection = connection
    this.sessionId = sessionId
    this.targetId = targetId
    /** @type {string[]} uncaught errors, and errors or warnings that the page logged, except failed requests */
    this.problems = []
    connection.listen((message) => {
      if (message.sessionId !== this.sessionId) return
      this.watch(message)
    })
  }

  /** @param {{ method: string, params: any }} message */
  watch({ method, params }) {
    if (method === 'Runtime.exceptionThrown') {
      const details = params.exceptionDetails
      this.problems.push(`exception: ${details.exception?.description ?? details.text}`)
    } else if (method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(params.type)) {
      this.problems.push(`console.${params.type}: ${params.args.map((arg) => arg.value ?? arg.description).join(' ')}`)
    } else if (method === 'Log.entryAdded') {
      const { level, source, text } = params.entry
      // A 4xx or 5xx that a test provokes on purpose is logged by the network stack, and is not a defect.
      if (['error', 'warning'].includes(level) && source !== 'network') {
        this.problems.push(`${source} ${level}: ${text}`)
      }
    }
  }

  /**
   * @param {string} method
   * @param {Record<string, unknown>} [params]
   */
  send(method, params = {}) {
    return this.connection.send(method, params, this.sessionId)
  }

  /**
   * @param {{ width: number, height: number }} size
   */
  async resize({ width, height }) {
    // Phones draw their scrollbars over the page; desktops reserve room for them.
    await this.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 600,
    })
  }

  /**
   * @param {string} source runs in every document of the tab, before the scripts of the page
   */
  async runBeforeLoad(source) {
    await this.send('Page.addScriptToEvaluateOnNewDocument', { source })
  }

  /**
   * @param {string} url
   */
  async goto(url) {
    const loaded = new Promise((resolve) => {
      const stop = this.connection.listen((message) => {
        if (message.sessionId === this.sessionId && message.method === 'Page.loadEventFired') {
          stop()
          resolve(undefined)
        }
      })
    })
    await this.send('Page.navigate', { url })
    await loaded
  }

  /**
   * Runs a function in the page. The function is serialized, so it can use nothing but its arguments
   * and the globals of the browser.
   *
   * @template T
   * @param {((...args: any[]) => T) | string} code
   * @param {...unknown} args
   * @returns {Promise<Awaited<T>>}
   */
  async evaluate(code, ...args) {
    const expression =
      typeof code === 'function' ? `(${code})(...${JSON.stringify(args)})` : code
    const { result, exceptionDetails } = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })
    if (exceptionDetails) {
      throw new Error(`in the page: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`)
    }
    return result.value
  }

  /**
   * Polls until the function returns something truthy, and returns it.
   *
   * @param {(...args: any[]) => any} predicate runs in the page
   * @param {{ timeout?: number, args?: unknown[], message?: string }} [options]
   */
  async waitFor(predicate, { timeout = 10_000, args = [], message = String(predicate) } = {}) {
    const deadline = Date.now() + timeout
    let last
    while (Date.now() < deadline) {
      last = await this.evaluate(predicate, ...args)
      if (last) return last
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
    const seen = this.problems.length > 0 ? `\nThe page reported:\n${this.problems.join('\n')}` : ''
    throw new Error(`Timed out waiting for: ${message}${seen}`)
  }

  /**
   * Presses the mouse in the middle of an element that is on the screen, like a person would.
   *
   * @param {string} selector
   */
  async click(selector) {
    const point = await this.evaluate((css) => {
      const element = document.querySelector(css)
      if (!element) return { error: `nothing matches ${css}` }
      const box = element.getBoundingClientRect()
      const x = box.left + box.width / 2
      const y = box.top + box.height / 2
      if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
        return { error: `${css} is off the screen at (${Math.round(x)}, ${Math.round(y)})` }
      }
      if (document.elementFromPoint(x, y)?.closest(css) === null) {
        return { error: `${css} is covered by something else at (${Math.round(x)}, ${Math.round(y)})` }
      }
      return { x, y }
    }, selector)
    if (point.error) throw new Error(point.error)
    const at = { x: point.x, y: point.y, button: 'left', clickCount: 1 }
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: at.x, y: at.y })
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...at })
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...at })
  }

  /**
   * @param {string} selector a file input
   * @param {string[]} files absolute paths
   */
  async chooseFiles(selector, files) {
    const { root } = await this.send('DOM.getDocument')
    const { nodeId } = await this.send('DOM.querySelector', { nodeId: root.nodeId, selector })
    if (!nodeId) throw new Error(`nothing matches ${selector}`)
    await this.send('DOM.setFileInputFiles', { nodeId, files })
  }

  /**
   * For debugging a failing test: writes what the tab shows to a PNG file.
   *
   * @param {string} path
   */
  async screenshot(path) {
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(path, Buffer.from(data, 'base64'))
  }

  async close() {
    await this.connection.send('Target.closeTarget', { targetId: this.targetId }).catch(() => undefined)
  }
}

/**
 * Starts a headless Chrome with a profile of its own, which is deleted when it quits.
 *
 * @param {{ executable?: string | null }} [options]
 * @returns {Promise<{ newPage: (size: { width: number, height: number }) => Promise<Page>, close: () => Promise<void> }>}
 */
export async function launchChrome({ executable = findChrome() } = {}) {
  if (!executable) throw new Error('Chrome was not found. Set CHROME_BIN to its executable.')
  const profile = mkdtempSync(join(tmpdir(), 'alexandria-chrome-'))
  const flags = [
    '--headless=new',
    '--remote-debugging-pipe',
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-sync',
    '--disable-dev-shm-usage',
    '--mute-audio',
    '--force-color-profile=srgb',
    '--lang=en-US',
    // Chrome refuses to start as root without this, which is how containers usually run.
    ...(process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    'about:blank',
  ]
  const child = spawn(executable, flags, { stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'] })
  let errors = ''
  child.stderr?.on('data', (chunk) => {
    errors = (errors + chunk).slice(-2000)
  })
  const exited = new Promise((resolve) => child.once('exit', resolve))
  const connection = new Connection(
    /** @type {import('node:stream').Readable} */ (child.stdio[4]),
    /** @type {import('node:stream').Writable} */ (child.stdio[3]),
  )
  child.once('exit', (code) => connection.fail(`Chrome exited with code ${code}`))

  const forget = () => rmSync(profile, { recursive: true, force: true, maxRetries: 3 })
  const close = async () => {
    if (child.exitCode === null) {
      await connection.send('Browser.close').catch(() => undefined)
      const killer = setTimeout(() => child.kill('SIGKILL'), EXIT_TIMEOUT_MS)
      await exited
      clearTimeout(killer)
    }
    forget()
  }

  let timer
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`Chrome did not answer in ${LAUNCH_TIMEOUT_MS / 1000}s`)),
        LAUNCH_TIMEOUT_MS,
      )
    })
    await Promise.race([connection.send('Browser.getVersion'), timeout])
    clearTimeout(timer)
  } catch (error) {
    clearTimeout(timer)
    child.kill('SIGKILL')
    forget()
    throw new Error(`${error instanceof Error ? error.message : error}${errors ? `\n${errors}` : ''}`)
  }

  return {
    async newPage(size) {
      const { targetId } = await connection.send('Target.createTarget', { url: 'about:blank' })
      const { sessionId } = await connection.send('Target.attachToTarget', { targetId, flatten: true })
      const page = new Page(connection, sessionId, targetId)
      await Promise.all(['Page', 'Runtime', 'Log', 'DOM'].map((domain) => page.send(`${domain}.enable`)))
      await page.resize(size)
      return page
    },
    close,
  }
}
