<script>
  import { statusLabel } from '../../lib/format.js'

  /** @type {{ id: string, versions: import('../../lib/types.js').VersionSummary[], value: string | null, latest?: string | null, onchange: (version: string) => void }} */
  let { id, versions, value, latest = null, onchange } = $props()
</script>

<div class="field version-select">
  <label class="field-label" for={id}>Version</label>
  <select
    {id}
    class="select"
    {value}
    onchange={(event) => onchange(event.currentTarget.value)}
  >
    {#each versions as item (item.version)}
      <option value={item.version}>
        {item.version}{item.version === latest ? ' (latest)' : ''} &middot; {statusLabel(item.status)}
      </option>
    {/each}
  </select>
</div>
