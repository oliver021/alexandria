<script>
  import Icon from './Icon.svelte'

  /** @type {{ tone?: 'info' | 'success' | 'warning' | 'danger', title?: string, live?: boolean, id?: string, focusable?: boolean, children?: import('svelte').Snippet, actions?: import('svelte').Snippet }} */
  let { tone = 'info', title = '', live = true, id, focusable = false, children, actions } = $props()

  /** @type {Record<string, string>} */
  const icons = {
    info: 'info',
    success: 'check-circle',
    warning: 'alert-triangle',
    danger: 'alert-circle',
  }
  const role = $derived(live ? (tone === 'danger' ? 'alert' : 'status') : undefined)
</script>

<!--
  `focusable` lets a page move the focus here with `focusAndReveal(id)` after something failed. The
  value is always -1 (the focus is moved by code, never by Tab), which the compiler cannot see
  through the condition.
-->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div class="alert alert-{tone}" {id} {role} tabindex={focusable ? -1 : undefined}>
  <span class="alert__icon"><Icon name={icons[tone] ?? 'info'} /></span>
  <div class="alert__body">
    {#if title}<p class="alert__title">{title}</p>{/if}
    {#if children}<div>{@render children()}</div>{/if}
    {#if actions}<div class="alert__actions">{@render actions()}</div>{/if}
  </div>
</div>
