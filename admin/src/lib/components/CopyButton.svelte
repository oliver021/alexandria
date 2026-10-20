<script>
  import { onDestroy } from 'svelte'
  import { copyText } from '../clipboard.js'
  import { toasts } from '../toast.svelte.js'
  import Icon from './Icon.svelte'

  /** @type {{ text: string, label?: string, copiedLabel?: string, small?: boolean, subject?: string }} */
  let { text, label = 'Copy', copiedLabel = 'Copied', small = true, subject = '' } = $props()

  let copied = $state(false)
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer

  async function copy() {
    const ok = await copyText(text)
    if (!ok) {
      toasts.error('Copying failed. Select the text and copy it manually.')
      return
    }
    copied = true
    clearTimeout(timer)
    timer = setTimeout(() => {
      copied = false
    }, 2000)
  }

  onDestroy(() => clearTimeout(timer))
</script>

<button type="button" class={['btn', small && 'btn-sm']} onclick={copy}>
  <Icon name={copied ? 'check' : 'copy'} />
  {copied ? copiedLabel : label}
  {#if subject}<span class="visually-hidden">{subject}</span>{/if}
</button>
<span class="visually-hidden" role="status">{copied ? 'Copied to the clipboard' : ''}</span>
