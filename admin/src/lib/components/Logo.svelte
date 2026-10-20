<script>
  /** @type {{ size?: number }} */
  let { size = 28 } = $props()
</script>

<svg
  class="brand__mark"
  width={size}
  height={size}
  viewBox="0 0 32 32"
  fill="none"
  stroke="currentColor"
  stroke-width="2.4"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  <path d="M7 26V15a9 9 0 0 1 18 0v11" />
  <path d="M12 26V16M16 26V12M20 26V16" />
</svg>
