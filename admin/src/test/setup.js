import '@testing-library/jest-dom/vitest'
import { vi } from 'vitest'

if (typeof HTMLDialogElement !== 'undefined') {
  if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
    HTMLDialogElement.prototype.showModal = function showModal() {
      this.setAttribute('open', '')
    }
  }
  if (typeof HTMLDialogElement.prototype.close !== 'function') {
    HTMLDialogElement.prototype.close = function close() {
      this.removeAttribute('open')
      this.dispatchEvent(new Event('close'))
    }
  }
}

// jsdom does not scroll. The browser and hosting tests run in plain Node, where there is no window.
if (typeof window !== 'undefined') {
  window.scrollTo = vi.fn()
  Element.prototype.scrollIntoView = vi.fn()
}
