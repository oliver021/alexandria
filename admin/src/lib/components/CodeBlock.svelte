<script>
  import CopyButton from './CopyButton.svelte'

  /** @type {{ label: string, code: string }} */
  let { label, code } = $props()
</script>

<figure class="codeblock">
  <figcaption class="codeblock__bar">
    <span>{label}</span>
    <CopyButton text={code} subject={label} />
  </figcaption>
  <pre class="codeblock__pre"><code>{code}</code></pre>
</figure>
