<script>
  import { statusLabel } from '../format.js'
  import Badge from './Badge.svelte'

  /** @type {{ status: string }} */
  let { status } = $props()

  /** @type {Record<string, string>} */
  const tones = {
    published: 'ok',
    draft: 'info',
    deprecated: 'warn',
    unpublished: 'neutral',
  }
</script>

<Badge tone={tones[status] ?? 'neutral'}>{statusLabel(status)}</Badge>
