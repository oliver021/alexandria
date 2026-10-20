<script>
  import { formatDate, formatDateTime, formatRelative } from '../format.js'

  /** @type {{ value: string | null | undefined, mode?: 'date' | 'datetime' | 'relative' }} */
  let { value, mode = 'date' } = $props()

  const text = $derived(
    mode === 'relative'
      ? formatRelative(value)
      : mode === 'datetime'
        ? formatDateTime(value)
        : formatDate(value),
  )
</script>

{#if value}
  <time datetime={value} title={formatDateTime(value)}>{text}</time>
{:else}
  <span class="faint" aria-label="none">&mdash;</span>
{/if}
