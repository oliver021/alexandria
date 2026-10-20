<script>
  /** @type {{ tabs: Array<{ id: string, label: string }>, selected: string, onselect: (id: string) => void, label: string, idPrefix?: string }} */
  let { tabs, selected, onselect, label, idPrefix = 'tabs' } = $props()

  /** @type {HTMLElement | undefined} */
  let list = $state()

  /**
   * @param {KeyboardEvent} event
   * @param {number} index
   */
  function onkeydown(event, index) {
    let next = -1
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    if (next === -1) return
    event.preventDefault()
    onselect(tabs[next].id)
    const buttons = list?.querySelectorAll('[role="tab"]')
    const target = buttons?.[next]
    if (target instanceof HTMLElement) target.focus()
  }
</script>

<div class="tabs" role="tablist" aria-label={label} bind:this={list}>
  {#each tabs as tab, index (tab.id)}
    <button
      type="button"
      role="tab"
      class="tab"
      id="{idPrefix}-tab-{tab.id}"
      aria-selected={tab.id === selected}
      aria-controls="{idPrefix}-panel-{tab.id}"
      tabindex={tab.id === selected ? 0 : -1}
      onclick={() => onselect(tab.id)}
      onkeydown={(event) => onkeydown(event, index)}
    >
      {tab.label}
    </button>
  {/each}
</div>
