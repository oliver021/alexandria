const STORAGE_KEY = 'alexandria.admin.session'

/**
 * @typedef {Object} StoredSession
 * @property {string} token
 * @property {import('./types.js').User} user
 */

/**
 * @returns {StoredSession | null}
 */
function readStored() {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const data = JSON.parse(raw)
    if (
      data &&
      typeof data.token === 'string' &&
      data.token !== '' &&
      data.user &&
      typeof data.user.id === 'string' &&
      typeof data.user.username === 'string'
    ) {
      return data
    }
  } catch {
    return null
  }
  return null
}

/**
 * @param {StoredSession | null} session
 */
function writeStored(session) {
  try {
    if (session) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session))
    else window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    return
  }
}

class Session {
  /** @type {string | null} */
  token = $state(null)

  /** @type {import('./types.js').User | null} */
  user = $state.raw(null)

  notice = $state('')

  constructor() {
    this.restore()
  }

  get isAuthenticated() {
    return this.token !== null
  }

  get isAdmin() {
    return this.user?.role === 'admin'
  }

  restore() {
    const stored = readStored()
    this.token = stored ? stored.token : null
    this.user = stored ? stored.user : null
  }

  /**
   * @param {string} token
   * @param {import('./types.js').User} user
   */
  start(token, user) {
    this.token = token
    this.user = user
    this.notice = ''
    writeStored({ token, user })
  }

  /**
   * @param {import('./types.js').User} user
   */
  updateUser(user) {
    this.user = user
    if (this.token) writeStored({ token: this.token, user })
  }

  /**
   * @param {string} [notice]
   */
  end(notice = '') {
    this.token = null
    this.user = null
    this.notice = notice
    writeStored(null)
  }

  clearNotice() {
    this.notice = ''
  }
}

export const session = new Session()
