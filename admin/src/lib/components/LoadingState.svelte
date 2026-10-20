<script>
  /** @type {{ label?: string, compact?: boolean }} */
  let { label = 'Loading…', compact = false } = $props()
</script>

<div class={['state', compact && 'state-compact']} role="status">
  <span class="spinner" aria-hidden="true"></span>
  <span>{label}</span>
</div>
