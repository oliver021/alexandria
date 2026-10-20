<script>
  import { ICONS } from './icons.js'

  /** @type {{ name: string, size?: number }} */
  let { name, size = 18 } = $props()

  const paths = $derived(ICONS[/** @type {keyof typeof ICONS} */ (name)] ?? [])
</script>

<svg
  class="icon"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  {#each paths as d (d)}
    <path {d} />
  {/each}
</svg>
