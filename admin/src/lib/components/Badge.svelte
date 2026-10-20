<script>
  /** @type {{ tone?: string, children?: import('svelte').Snippet }} */
  let { tone = 'neutral', children } = $props()
</script>

<span class="badge badge-{tone}">{@render children?.()}</span>
