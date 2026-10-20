import { hrefFor, resolveHash } from './routes.js'

const TITLE_SUFFIX = 'Alexandria Admin'

function currentHash() {
  return typeof window === 'undefined' ? '' : window.location.hash
}

class Router {
  route = $state.raw(resolveHash(currentHash()))

  #listening = false

  #sync = () => {
    const next = resolveHash(currentHash())
    if (next.hash !== this.route.hash || next.name !== this.route.name) {
      this.route = next
    }
  }

  start() {
    if (!this.#listening) {
      window.addEventListener('hashchange', this.#sync)
      this.#listening = true
    }
    this.#sync()
  }

  stop() {
    if (this.#listening) {
      window.removeEventListener('hashchange', this.#sync)
      this.#listening = false
    }
  }

  /**
   * @param {string} path
   * @param {{ query?: import('./routes.js').QueryInput | null, replace?: boolean }} [options]
   */
  navigate(path, { query = null, replace = false } = {}) {
    const hash = hrefFor(path, query)
    if (replace) {
      window.history.replaceState(window.history.state, '', hash)
    } else if (window.location.hash !== hash) {
      window.location.hash = hash.slice(1)
    }
    this.#sync()
  }
}

export const router = new Router()

/**
 * @param {string} [title]
 */
export function setPageTitle(title) {
  document.title = title ? `${title} · ${TITLE_SUFFIX}` : TITLE_SUFFIX
}
