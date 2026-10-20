<script>
  /** @type {{ title: string, lead?: string, actions?: import('svelte').Snippet, children?: import('svelte').Snippet }} */
  let { title, lead = '', actions, children } = $props()
</script>

<div class="page-head">
  <div class="page-head__text">
    <h1>{title}</h1>
    {#if lead}<p class="page-head__lead">{lead}</p>{/if}
    {@render children?.()}
  </div>
  {#if actions}
    <div class="page-head__actions">{@render actions()}</div>
  {/if}
</div>
