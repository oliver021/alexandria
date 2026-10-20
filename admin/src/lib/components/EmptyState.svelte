<script>
  import Icon from './Icon.svelte'

  /** @type {{ icon?: string, title: string, text?: string, heading?: 'p' | 'h1' | 'h2', children?: import('svelte').Snippet }} */
  let { icon = 'package', title, text = '', heading = 'p', children } = $props()
</script>

<div class="state">
  <Icon name={icon} size={28} />
  <svelte:element this={heading} class="state__title">{title}</svelte:element>
  {#if text}<p class="state__text">{text}</p>{/if}
  {#if children}<div class="state__actions">{@render children()}</div>{/if}
</div>
