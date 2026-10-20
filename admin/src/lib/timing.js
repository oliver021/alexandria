/**
 * @template {(...args: any[]) => void} F
 * @param {F} callback
 * @param {number} delay
 * @returns {F & { cancel: () => void, flush: () => void }}
 */
export function debounce(callback, delay) {
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer
  /** @type {Parameters<F> | undefined} */
  let pending

  const run = () => {
    timer = undefined
    const args = pending
    pending = undefined
    if (args) callback(...args)
  }

  const debounced = /** @type {F & { cancel: () => void, flush: () => void }} */ (
    /** @type {unknown} */ (
      (/** @type {Parameters<F>} */ ...args) => {
        pending = args
        if (timer !== undefined) clearTimeout(timer)
        timer = setTimeout(run, delay)
      }
    )
  )
  debounced.cancel = () => {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
    pending = undefined
  }
  debounced.flush = () => {
    if (timer !== undefined) {
      clearTimeout(timer)
      run()
    }
  }
  return debounced
}
