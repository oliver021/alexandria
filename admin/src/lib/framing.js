export const FRAMED_MESSAGE =
  'Alexandria Admin cannot be shown inside another page. Open it in its own tab.'

/**
 * @param {Window} [win]
 * @returns {boolean}
 */
export function isFramed(win = window) {
  try {
    return win.top !== win.self
  } catch {
    return true
  }
}
