<script>
  import { kindLabel } from '../format.js'
  import Badge from './Badge.svelte'

  /** @type {{ kind: string }} */
  let { kind } = $props()
</script>

<Badge tone={kind === 'grounding' ? 'grounding' : 'skill'}>{kindLabel(kind)}</Badge>
