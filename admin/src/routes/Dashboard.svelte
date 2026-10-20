<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  import { session } from '../lib/auth.svelte.js'
  import Badge from '../lib/components/Badge.svelte'
  import CopyButton from '../lib/components/CopyButton.svelte'
  import EmptyState from '../lib/components/EmptyState.svelte'
  import ErrorState from '../lib/components/ErrorState.svelte'
  import Icon from '../lib/components/Icon.svelte'
  import KindBadge from '../lib/components/KindBadge.svelte'
  import PageHeader from '../lib/components/PageHeader.svelte'
  import StatusBadge from '../lib/components/StatusBadge.svelte'
  import Time from '../lib/components/Time.svelte'
  import { formatBytes, formatNumber } from '../lib/format.js'
  import { describeError } from '../lib/reasons.js'
  import { useResource } from '../lib/resource.svelte.js'
  import { entryHref, hrefFor, paths } from '../lib/routes.js'
  import { joinUrl } from '../lib/snippets.js'

  const COUNT_QUERIES = {
    total: {},
    skill: { kind: 'skill' },
    grounding: { kind: 'grounding' },
    published: { status: 'published' },
    draft: { status: 'draft' },
    hidden: { status: 'unpublished' },
    mine: { mine: true },
  }

  const counts = useResource(async (signal) => {
    const keys = /** @type {Array<keyof typeof COUNT_QUERIES>} */ (Object.keys(COUNT_QUERIES))
    const settled = await Promise.allSettled(
      keys.map((key) => api.listEntries({ limit: 1, ...COUNT_QUERIES[key] }, { signal })),
    )
    /** @type {Record<string, number | null>} */
    const result = {}
    /** @type {unknown} */
    let failure = null
    settled.forEach((outcome, index) => {
      if (outcome.status === 'fulfilled') {
        result[keys[index]] = outcome.value.total
      } else {
        result[keys[index]] = null
        failure ??= outcome.reason
      }
    })
    if (failure && Object.values(result).every((value) => value === null)) throw failure
    return result
  })
  const recent = useResource((signal) =>
    api.listEntries({ limit: 6, sort: 'updated' }, { signal }),
  )
  const info = useResource((signal) => api.info({ signal }))

  onMount(() => {
    counts.load()
    recent.load()
    info.load()
  })

  const tiles = [
    { key: 'total', label: 'Entries you can see', query: null },
    { key: 'skill', label: 'Skills', query: { kind: 'skill' } },
    { key: 'grounding', label: 'Grounding packages', query: { kind: 'grounding' } },
    { key: 'mine', label: 'Owned by you', query: { mine: true } },
  ]

  const total = $derived(counts.data?.total ?? 0)
  const parts = $derived(
    [
      { key: 'published', label: 'Published', tone: 'ok', status: 'published' },
      { key: 'draft', label: 'Draft', tone: 'warn', status: 'draft' },
      { key: 'hidden', label: 'Hidden', tone: 'neutral', status: 'unpublished' },
    ].map((part) => ({
      ...part,
      value: counts.data?.[part.key] ?? null,
      width: total > 0 ? ((counts.data?.[part.key] ?? 0) / total) * 100 : 0,
    })),
  )
  const mcpUrl = $derived(info.data ? joinUrl(info.data.public_url, info.data.mcp_path) : '')
</script>

<PageHeader
  title="Dashboard"
  lead="Welcome back, {session.user?.username ?? 'there'}. This is the registry as you can see it."
>
  {#snippet actions()}
    <a class="btn btn-primary" href={hrefFor(paths.upload())}>
      <Icon name="upload" />
      Upload package
    </a>
  {/snippet}
</PageHeader>

<div class="stack-lg">
  {#if counts.error}
    <div class="card">
      <ErrorState error={counts.error} title="The totals could not be loaded" onretry={counts.reload} />
    </div>
  {:else}
    <section aria-labelledby="dashboard-totals">
      <h2 id="dashboard-totals" class="visually-hidden">Totals</h2>
      <div class="stat-grid" aria-busy={counts.loading}>
        {#each tiles as tile (tile.key)}
          <a class="stat" href={hrefFor(paths.library(), tile.query)}>
            <span class="stat__value">
              {#if counts.data}
                {formatNumber(counts.data[tile.key])}
              {:else}
                <span class="skeleton stat__skeleton"></span>
                <span class="visually-hidden">Loading</span>
              {/if}
            </span>
            <span class="stat__label">{tile.label}</span>
          </a>
        {/each}
      </div>
    </section>

    <section class="card" aria-labelledby="dashboard-status">
      <div class="card__head">
        <h2 id="dashboard-status">Entries by status</h2>
      </div>
      <div class="card__body stack">
        {#if counts.data && total === 0}
          <p class="muted">No entries yet, so there is nothing to break down.</p>
        {:else}
          <div class="meter" aria-hidden="true">
            {#each parts as part (part.key)}
              <span class="meter__part meter__part-{part.tone}" style:width="{part.width}%"></span>
            {/each}
          </div>
          <ul class="legend">
            {#each parts as part (part.key)}
              <li>
                <span class="legend__swatch meter__part-{part.tone}" aria-hidden="true"></span>
                <a href={hrefFor(paths.library(), { status: part.status })}>{part.label}</a>
                <strong>{part.value === null ? '—' : formatNumber(part.value)}</strong>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </section>
  {/if}

  <div class="two-col">
    <section class="card" aria-labelledby="dashboard-recent">
      <div class="card__head">
        <h2 id="dashboard-recent">Recently updated</h2>
        <a href={hrefFor(paths.library(), { sort: 'updated' })}>View all</a>
      </div>
      {#if recent.error}
        <ErrorState error={recent.error} onretry={recent.reload} />
      {:else if !recent.loaded}
        <div class="card__body stack" aria-busy="true">
          {#each [0, 1, 2, 3] as row (row)}
            <span class="skeleton"></span>
          {/each}
        </div>
      {:else if recent.data && recent.data.items.length === 0}
        <EmptyState
          title="No entries yet"
          text="Upload a package to publish the first skill or grounding entry."
        >
          <a class="btn btn-primary" href={hrefFor(paths.upload())}>Upload package</a>
        </EmptyState>
      {:else if recent.data}
        <ul class="recent">
          {#each recent.data.items as entry (entry.id)}
            <li class="recent__item">
              <div class="recent__main">
                <a class="recent__name" href={entryHref(entry.name)}>
                  {entry.display_name}
                </a>
                <span class="mono muted wrap-anywhere">{entry.name}</span>
              </div>
              <div class="cluster">
                <KindBadge kind={entry.kind} />
                {#if entry.status !== 'published'}<StatusBadge status={entry.status} />{/if}
                <span class="muted small nowrap">
                  <Time value={entry.updated_at} mode="relative" />
                </span>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <section class="card" aria-labelledby="dashboard-actions">
      <div class="card__head">
        <h2 id="dashboard-actions">Quick access</h2>
      </div>
      <ul class="quick">
        <li>
          <a href={hrefFor(paths.upload())}><Icon name="upload" />Upload a package</a>
        </li>
        <li>
          <a href={hrefFor(paths.library())}><Icon name="library" />Browse the library</a>
        </li>
        <li>
          <a href={hrefFor(paths.tokens())}><Icon name="key" />Create an API token</a>
        </li>
        {#if session.isAdmin}
          <li>
            <a href={hrefFor(paths.users())}><Icon name="users" />Manage users</a>
          </li>
        {/if}
        <li>
          <a href="/registry"><Icon name="globe" />Open the public registry</a>
        </li>
        <li>
          <a href="/docs"><Icon name="external" />Read the API reference</a>
        </li>
      </ul>
    </section>
  </div>

  <section class="card" aria-labelledby="dashboard-registry">
    <div class="card__head">
      <h2 id="dashboard-registry">Registry</h2>
      {#if info.data}
        <Badge tone="ok">Online</Badge>
      {/if}
    </div>
    {#if info.error}
      <div class="card__body stack">
        <p class="field-error" role="alert">{describeError(info.error).message}</p>
        <div>
          <button type="button" class="btn btn-sm" onclick={() => info.reload()}>Try again</button>
        </div>
      </div>
    {:else if !info.data}
      <div class="card__body stack" aria-busy="true">
        <span class="skeleton"></span>
        <span class="skeleton"></span>
        <span class="skeleton"></span>
      </div>
    {:else}
      <div class="card__body">
        <dl class="dl dl-registry">
          <dt>Version</dt>
          <dd>{info.data.name} {info.data.version}</dd>
          <dt>Reading</dt>
          <dd>
            {#if info.data.public_read}
              <Badge tone="info">Public</Badge>
              <span class="muted small">Anyone can read published entries.</span>
            {:else}
              <Badge tone="warn">Private</Badge>
              <span class="muted small">Every read needs a credential.</span>
            {/if}
          </dd>
          <dt>MCP endpoint</dt>
          <dd>
            <div class="stack">
              <code>{mcpUrl}</code>
              <div><CopyButton text={mcpUrl} label="Copy URL" subject="MCP endpoint" /></div>
            </div>
          </dd>
          <dt>Upload limits</dt>
          <dd>
            {formatBytes(info.data.limits.max_package_bytes)} per package,
            {formatNumber(info.data.limits.max_files)} files,
            {formatBytes(info.data.limits.max_uncompressed_bytes)} unpacked
          </dd>
        </dl>
      </div>
    {/if}
  </section>
</div>
