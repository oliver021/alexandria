<script>
  import { untrack } from 'svelte'

  /** @type {{ open?: boolean, title: string, confirmLabel?: string, cancelLabel?: string, tone?: 'default' | 'danger', busy?: boolean, error?: string, onconfirm?: () => void, oncancel?: () => void, children?: import('svelte').Snippet }} */
  let {
    open = false,
    title,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    tone = 'default',
    busy = false,
    error = '',
    onconfirm,
    oncancel,
    children,
  } = $props()

  const uid = $props.id()

  /** @type {HTMLDialogElement | undefined} */
  let dialog = $state()
  /** @type {HTMLButtonElement | undefined} */
  let cancelButton = $state()

  $effect(() => {
    if (!open || !dialog) return undefined
    const element = dialog
    const previous = document.activeElement
    if (typeof element.showModal === 'function') {
      if (!element.open) element.showModal()
    } else {
      element.setAttribute('open', '')
    }
    untrack(() => cancelButton)?.focus()
    return () => {
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus()
    }
  })
</script>

{#if open}
  <dialog
    bind:this={dialog}
    class="dialog"
    aria-labelledby="{uid}-title"
    aria-describedby="{uid}-body"
    oncancel={(event) => {
      event.preventDefault()
      if (!busy) oncancel?.()
    }}
    onclose={() => {
      if (!busy) oncancel?.()
    }}
  >
    <div class="dialog__body">
      <h2 id="{uid}-title">{title}</h2>
      <div id="{uid}-body" class="stack">{@render children?.()}</div>
      {#if error}
        <p class="field-error" role="alert">{error}</p>
      {/if}
    </div>
    <div class="dialog__actions">
      <button
        type="button"
        class="btn"
        bind:this={cancelButton}
        disabled={busy}
        onclick={() => oncancel?.()}
      >
        {cancelLabel}
      </button>
      <button
        type="button"
        class={['btn', tone === 'danger' ? 'btn-danger' : 'btn-primary']}
        disabled={busy}
        onclick={() => onconfirm?.()}
      >
        {#if busy}<span class="spinner" aria-hidden="true"></span>{/if}
        {confirmLabel}
      </button>
    </div>
  </dialog>
{/if}
