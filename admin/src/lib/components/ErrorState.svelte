<script>
  import { describeError } from '../reasons.js'
  import Icon from './Icon.svelte'

  /** @type {{ error: unknown, title?: string, heading?: 'p' | 'h1' | 'h2', onretry?: () => unknown, children?: import('svelte').Snippet }} */
  let { error, title = '', heading = 'p', onretry, children } = $props()

  const info = $derived(describeError(error))
</script>

<div class="state state-error" role="alert">
  <Icon name="alert-circle" size={28} />
  <svelte:element this={heading} class="state__title">{title || info.title}</svelte:element>
  <p class="state__text">{info.message}</p>
  {#if info.hint}<p class="state__text">{info.hint}</p>{/if}
  {#if onretry || children}
    <div class="state__actions">
      {#if onretry}
        <button type="button" class="btn" onclick={() => onretry()}>
          <Icon name="refresh" />
          Try again
        </button>
      {/if}
      {@render children?.()}
    </div>
  {/if}
</div>
