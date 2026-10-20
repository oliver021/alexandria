<script>
  import Alert from '../../lib/components/Alert.svelte'
  import CodeBlock from '../../lib/components/CodeBlock.svelte'
  import { encodeSegment } from '../../lib/paths.js'
  import { hrefFor, paths } from '../../lib/routes.js'
  import { buildSnippets, joinUrl } from '../../lib/snippets.js'
  import VersionSelect from './VersionSelect.svelte'

  /** @type {{ entry: import('../../lib/types.js').EntryDetail, version: string | null, versionStatus?: string, info?: import('../../lib/types.js').RegistryInfo | null, detail?: import('../../lib/types.js').VersionDetail | null, onversion: (version: string) => void }} */
  let { entry, version, versionStatus = '', info = null, detail = null, onversion } = $props()

  const publicUrl = $derived(info?.public_url ?? window.location.origin)
  const mcpPath = $derived(info?.mcp_path ?? '/mcp')
  const downloadUrl = $derived(
    detail && detail.version === version
      ? detail.download_url
      : joinUrl(
          publicUrl,
          `/api/v1/skills/${encodeSegment(entry.name)}/versions/${encodeSegment(version ?? 'latest')}/download`,
        ),
  )
  const snippets = $derived(
    buildSnippets({
      publicUrl,
      mcpPath,
      name: entry.name,
      version: version ?? 'latest',
      downloadUrl,
    }),
  )
</script>

<div class="stack-lg">
  <div class="files-bar">
    <VersionSelect
      id="use-version"
      versions={entry.versions}
      value={version}
      latest={entry.latest_version}
      onchange={onversion}
    />
  </div>

  <Alert tone="info" live={false} title="Use an API token, not your session">
    <p>
      The examples use the placeholder <code>&lt;YOUR_API_TOKEN&gt;</code>. Create a
      <strong>read</strong> token on the
      <a href={hrefFor(paths.tokens())}>API tokens</a> page for agents that only consume entries.
      {#if info && !info.public_read}
        This registry is private, so every request needs the header.
      {:else}
        Published entries can also be read without a header, but the header unlocks your drafts.
      {/if}
    </p>
  </Alert>

  {#if versionStatus === 'draft'}
    <Alert tone="warning" live={false} title="This version is a draft">
      <p>Only its owner and admins can fetch it. Consumers will not see it until it is published.</p>
    </Alert>
  {:else if versionStatus === 'deprecated'}
    <Alert tone="warning" live={false} title="This version is deprecated">
      <p>It can still be fetched by its exact version, but it is never resolved as latest.</p>
    </Alert>
  {/if}

  <section class="stack" aria-labelledby="use-mcp">
    <h2 id="use-mcp">Connect an agent over MCP</h2>
    <p class="muted">
      The registry speaks MCP over Streamable HTTP at <code>{snippets.mcpUrl}</code>. Once an agent
      is connected it can call <code>get_entrypoint</code> with the name
      <code>{entry.name}</code> to read the instructions.
    </p>
    <CodeBlock label="MCP client configuration (JSON)" code={snippets.mcpJson} />
    <CodeBlock label="Claude Code" code={snippets.claudeAdd} />
  </section>

  <section class="stack" aria-labelledby="use-download">
    <h2 id="use-download">Download the package</h2>
    <CodeBlock label="curl" code={snippets.curl} />
    <p class="muted">
      Or pull it with the <code>alx</code> command line, which checks the SHA-256 and extracts only
      the files listed in the manifest.
    </p>
    <CodeBlock label="alx" code={`${snippets.alxEnv}\n${snippets.alx}`} />
  </section>
</div>
