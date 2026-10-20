const DEFAULT_TIMEOUTS = { success: 5000, info: 6000, warning: 9000, error: 0 }

/**
 * @typedef {'success' | 'info' | 'warning' | 'error'} ToastTone
 */

/**
 * @typedef {Object} Toast
 * @property {number} id
 * @property {ToastTone} tone
 * @property {string} message
 */

class ToastStore {
  /** @type {Toast[]} */
  items = $state([])

  #sequence = 0

  /** @type {Map<number, ReturnType<typeof setTimeout>>} */
  #timers = new Map()

  /**
   * @param {string} message
   * @param {{ tone?: ToastTone, timeout?: number }} [options]
   * @returns {number}
   */
  show(message, { tone = 'info', timeout } = {}) {
    this.#sequence += 1
    const id = this.#sequence
    this.items.push({ id, tone, message })
    const delay = timeout ?? DEFAULT_TIMEOUTS[tone]
    if (delay > 0) {
      this.#timers.set(
        id,
        setTimeout(() => this.dismiss(id), delay),
      )
    }
    return id
  }

  /** @param {string} message */
  success(message) {
    return this.show(message, { tone: 'success' })
  }

  /** @param {string} message */
  info(message) {
    return this.show(message, { tone: 'info' })
  }

  /** @param {string} message */
  warning(message) {
    return this.show(message, { tone: 'warning' })
  }

  /** @param {string} message */
  error(message) {
    return this.show(message, { tone: 'error' })
  }

  /** @param {number} id */
  dismiss(id) {
    const timer = this.#timers.get(id)
    if (timer !== undefined) {
      clearTimeout(timer)
      this.#timers.delete(id)
    }
    const index = this.items.findIndex((toast) => toast.id === id)
    if (index !== -1) this.items.splice(index, 1)
  }

  clear() {
    for (const timer of this.#timers.values()) clearTimeout(timer)
    this.#timers.clear()
    this.items.splice(0, this.items.length)
  }
}

export const toasts = new ToastStore()
