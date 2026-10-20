<script>
  import { toasts } from '../toast.svelte.js'
  import Icon from './Icon.svelte'

  const icons = {
    success: 'check-circle',
    info: 'info',
    warning: 'alert-triangle',
    error: 'alert-circle',
  }
</script>

<div class="toasts" role="status" aria-live="polite" aria-atomic="false" aria-relevant="additions">
  {#each toasts.items as toast (toast.id)}
    <div class="toast toast-{toast.tone}">
      <span class="toast__icon"><Icon name={icons[toast.tone]} /></span>
      <p class="toast__text">{toast.message}</p>
      <button
        type="button"
        class="btn btn-quiet btn-icon btn-sm"
        aria-label="Dismiss notification"
        onclick={() => toasts.dismiss(toast.id)}
      >
        <Icon name="x" />
      </button>
    </div>
  {/each}
</div>
