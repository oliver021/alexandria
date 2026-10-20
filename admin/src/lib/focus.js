import { tick } from 'svelte'

/**
 * @param {string} id
 * @returns {Promise<void>}
 */
export async function focusById(id) {
  await tick()
  const element = document.getElementById(id)
  if (element instanceof HTMLElement) element.focus()
}

/**
 * The part of the window that the sticky header does not cover, which is where `scrollIntoView` puts
 * things because `layout.css` sets `scroll-padding-top` to the height of the header.
 *
 * @returns {number} in pixels
 */
function visibleHeight() {
  const covered = Number.parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)
  return window.innerHeight - (Number.isFinite(covered) ? covered : 0)
}

/**
 * Moves the focus to an element and scrolls it to the middle of the window.
 *
 * A message about a form (a failed upload, a rejected field) is drawn above or inside the form, while
 * the button that caused it is lower down. After the submit the page can be scrolled so that the
 * message sits above the visible area, and then nothing on the screen says that something went wrong.
 * So the focus goes to the message, where a screen reader announces it, and the window scrolls to it.
 * The focus moves without scrolling, so that the browser does not scroll twice. An element that does
 * not fit in the visible part of the window is aligned to its top instead, so that its first lines
 * are the ones that show.
 *
 * @param {Element | null | undefined} element
 * @returns {boolean} false when there was nothing to focus
 */
function reveal(element) {
  if (!(element instanceof HTMLElement)) return false
  element.focus({ preventScroll: true })
  const fits = element.getBoundingClientRect().height <= visibleHeight()
  element.scrollIntoView({ block: fits ? 'center' : 'start', inline: 'nearest' })
  return true
}

/**
 * Focuses the element with the given id, once the pending updates are in the DOM, and brings it into
 * view. The element needs `tabindex="-1"` unless it is a control.
 *
 * @param {string} id
 * @returns {Promise<boolean>} false when the page has no such element
 */
export async function focusAndReveal(id) {
  await tick()
  return reveal(document.getElementById(id))
}

/**
 * Focuses the first control of a form that is marked `aria-invalid="true"` and brings it into view.
 *
 * @param {ParentNode | null | undefined} form
 * @returns {Promise<boolean>} false when no control is invalid
 */
export async function focusFirstInvalid(form) {
  await tick()
  return reveal(form?.querySelector('[aria-invalid="true"]'))
}
