import { onDestroy } from 'svelte'
import { isAbortError, isUnauthorized } from './api.js'

/**
 * @template T
 * @template {any[]} A
 * @param {(signal: AbortSignal, ...args: A) => Promise<T>} fetcher
 */
export function createResource(fetcher) {
  /** @type {T | undefined} */
  let data = $state.raw(undefined)
  /** @type {unknown} */
  let error = $state.raw(null)
  let loading = $state(false)
  let loaded = $state(false)
  /** @type {AbortController | null} */
  let controller = null
  /** @type {A | []} */
  let lastArgs = []

  /**
   * @param {A} args
   * @returns {Promise<T | undefined>}
   */
  async function load(...args) {
    lastArgs = args
    controller?.abort()
    const current = new AbortController()
    controller = current
    loading = true
    error = null
    try {
      const result = await fetcher(current.signal, ...args)
      if (controller !== current) return undefined
      data = result
      loaded = true
      return result
    } catch (cause) {
      if (controller !== current || isAbortError(cause) || isUnauthorized(cause)) return undefined
      error = cause
      return undefined
    } finally {
      if (controller === current) loading = false
    }
  }

  function abort() {
    controller?.abort()
    controller = null
    loading = false
  }

  return {
    get data() {
      return data
    },
    get error() {
      return error
    },
    get loading() {
      return loading
    },
    get loaded() {
      return loaded
    },
    load,
    reload: () => load(.../** @type {A} */ (lastArgs)),
    abort,
    /** @param {T | undefined} value */
    set(value) {
      data = value
      loaded = true
    },
  }
}

/**
 * @template T
 * @template {any[]} A
 * @param {(signal: AbortSignal, ...args: A) => Promise<T>} fetcher
 */
export function useResource(fetcher) {
  const resource = createResource(fetcher)
  onDestroy(resource.abort)
  return resource
}
