<script>
  import { onMount, untrack } from 'svelte'
  import { api } from '../lib/api.js'
  import EmptyState from '../lib/components/EmptyState.svelte'
  import ErrorState from '../lib/components/ErrorState.svelte'
  import Icon from '../lib/components/Icon.svelte'
  import KindBadge from '../lib/components/KindBadge.svelte'
  import PageHeader from '../lib/components/PageHeader.svelte'
  import Pagination from '../lib/components/Pagination.svelte'
  import StatusBadge from '../lib/components/StatusBadge.svelte'
  import Time from '../lib/components/Time.svelte'
  import { formatNumber } from '../lib/format.js'
  import {
    PAGE_SIZE,
    activeFilterCount,
    apiParamsFromFilters,
    filtersFromQuery,
    pageCountFor,
    queryFromFilters,
  } from '../lib/library-filters.js'
  import { useResource } from '../lib/resource.svelte.js'
  import { router } from '../lib/router.svelte.js'
  import { entryHref, hrefFor, paths } from '../lib/routes.js'
  import { debounce } from '../lib/timing.js'

  const SEARCH_DELAY_MS = 300

  const filters = $derived(filtersFromQuery(router.route.query))
  const activeCount = $derived(activeFilterCount(filters))
  const effectiveSort = $derived(filters.sort || (filters.q ? 'relevance' : 'name'))

  const results = useResource((signal, params) => api.listEntries(params, { signal }))
  const tagList = useResource((signal) => api.listTags({ limit: 100 }, { signal }))
  const categoryList = useResource((signal) => api.listCategories({ limit: 100 }, { signal }))

  let searchText = $state(untrack(() => filters.q))
  let lastPushedQuery = untrack(() => filters.q)
  let tagFilter = $state('')
  /** @type {HTMLDetailsElement | undefined} */
  let tagPicker = $state()

  const tagOptions = $derived.by(() => {
    const names = new Set((tagList.data ?? []).map((item) => item.name))
    /** @type {Array<{ name: string, count: number | null }>} */
    const options = [...(tagList.data ?? [])]
    for (const tag of filters.tags) {
      if (!names.has(tag)) options.push({ name: tag, count: null })
    }
    const needle = tagFilter.trim().toLowerCase()
    return needle ? options.filter((item) => item.name.includes(needle)) : options
  })
  const categoryOptions = $derived.by(() => {
    /** @type {Array<{ name: string, count: number | null }>} */
    const options = [...(categoryList.data ?? [])]
    if (filters.category && !options.some((item) => item.name === filters.category)) {
      options.push({ name: filters.category, count: null })
    }
    return options
  })

  const total = $derived(results.data?.total ?? 0)
  const pageCount = $derived(pageCountFor(total))

  onMount(() => {
    tagList.load()
    categoryList.load()
    return () => pushSearch.cancel()
  })

  $effect(() => {
    if (router.route.name !== 'library') return
    const params = apiParamsFromFilters(filters)
    untrack(() => results.load(params))
  })

  $effect(() => {
    const current = filters.q
    untrack(() => {
      if (current !== lastPushedQuery) {
        lastPushedQuery = current
        searchText = current
      }
    })
  })

  $effect(() => {
    const element = tagPicker
    if (!element) return undefined
    /** @param {MouseEvent} event */
    const closeOnOutsideClick = (event) => {
      if (element.open && event.target instanceof Node && !element.contains(event.target)) {
        element.open = false
      }
    }
    /** @param {KeyboardEvent} event */
    const closeOnEscape = (event) => {
      if (event.key === 'Escape' && element.open) {
        element.open = false
        element.querySelector('summary')?.focus()
      }
    }
    document.addEventListener('click', closeOnOutsideClick)
    element.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('click', closeOnOutsideClick)
      element.removeEventListener('keydown', closeOnEscape)
    }
  })

  $effect(() => {
    if (results.data && !results.loading && filters.page > pageCount) {
      untrack(() => apply({ page: pageCount }))
    }
  })

  /**
   * @param {Partial<import('../lib/library-filters.js').LibraryFilters>} patch
   */
  function apply(patch) {
    const next = { ...filters, ...patch }
    if (!('page' in patch)) next.page = 1
    router.navigate(paths.library(), { query: queryFromFilters(next), replace: true })
  }

  const pushSearch = debounce((/** @type {string} */ text) => {
    const clean = text.trim()
    lastPushedQuery = clean
    apply({ q: clean })
  }, SEARCH_DELAY_MS)

  /**
   * @param {SubmitEvent} event
   */
  function submitSearch(event) {
    event.preventDefault()
    pushSearch.cancel()
    const clean = searchText.trim()
    lastPushedQuery = clean
    apply({ q: clean })
  }

  /**
   * @param {string} tag
   * @param {boolean} checked
   */
  function toggleTag(tag, checked) {
    const tags = checked
      ? [...filters.tags.filter((item) => item !== tag), tag]
      : filters.tags.filter((item) => item !== tag)
    apply({ tags })
  }

  function clearFilters() {
    pushSearch.cancel()
    searchText = ''
    lastPushedQuery = ''
    router.navigate(paths.library(), { replace: true })
  }

  /**
   * @param {number} page
   */
  function goToPage(page) {
    apply({ page })
    document.getElementById('library-results')?.focus()
  }
</script>

<PageHeader
  title="Library"
  lead="Search the skills and grounding packages you can see. Filters are kept in the address, so a filtered view can be shared or bookmarked."
>
  {#snippet actions()}
    <a class="btn btn-primary" href={hrefFor(paths.upload())}>
      <Icon name="upload" />
      Upload package
    </a>
  {/snippet}
</PageHeader>

<form class="card filters" role="search" aria-label="Filter the library" onsubmit={submitSearch}>
  <div class="card__body stack">
    <div class="filters__row">
      <div class="field filters__search">
        <label class="field-label" for="library-q">Search</label>
        <div class="input-with-action">
          <input
            id="library-q"
            class="input"
            type="search"
            name="q"
            autocomplete="off"
            placeholder="Name, tag or text"
            value={searchText}
            oninput={(event) => {
              searchText = event.currentTarget.value
              pushSearch(searchText)
            }}
          />
          <button type="submit" class="btn">
            <Icon name="search" />
            Search
          </button>
        </div>
      </div>
      <div class="field">
        <label class="field-label" for="library-kind">Kind</label>
        <select
          id="library-kind"
          class="select"
          value={filters.kind}
          onchange={(event) => apply({ kind: event.currentTarget.value })}
        >
          <option value="">All kinds</option>
          <option value="skill">Skills</option>
          <option value="grounding">Grounding</option>
        </select>
      </div>
      <div class="field">
        <label class="field-label" for="library-category">Category</label>
        <select
          id="library-category"
          class="select"
          value={filters.category}
          onchange={(event) => apply({ category: event.currentTarget.value })}
        >
          <option value="">All categories</option>
          {#each categoryOptions as item (item.name)}
            <option value={item.name}>
              {item.name}{item.count === null ? '' : ` (${formatNumber(item.count)})`}
            </option>
          {/each}
        </select>
      </div>
      <div class="field">
        <label class="field-label" for="library-status">Status</label>
        <select
          id="library-status"
          class="select"
          value={filters.status}
          onchange={(event) => apply({ status: event.currentTarget.value })}
        >
          <option value="">Any status</option>
          <option value="published">Published</option>
          <option value="draft">Draft</option>
          <option value="unpublished">Hidden</option>
        </select>
      </div>
      <div class="field">
        <label class="field-label" for="library-sort">Sort by</label>
        <select
          id="library-sort"
          class="select"
          value={effectiveSort}
          onchange={(event) => apply({ sort: event.currentTarget.value })}
        >
          <option value="relevance" disabled={!filters.q}>Best match</option>
          <option value="name">Name</option>
          <option value="updated">Recently updated</option>
          <option value="created">Newest first</option>
        </select>
      </div>
    </div>

    <div class="filters__row filters__row-secondary">
      <details class="tagpicker" bind:this={tagPicker}>
        <summary class="btn">
          <Icon name="folder" />
          Tags{filters.tags.length > 0 ? ` (${filters.tags.length})` : ''}
        </summary>
        <div class="tagpicker__panel card">
          <div class="stack">
            {#if tagList.error}
              <p class="field-error" role="alert">The tags could not be loaded.</p>
            {:else if !tagList.loaded}
              <p class="muted">Loading tags&hellip;</p>
            {:else if tagOptions.length === 0 && tagFilter === ''}
              <p class="muted">No entry has tags yet.</p>
            {:else}
              <div class="field">
                <label class="field-label" for="library-tag-filter">Find a tag</label>
                <input
                  id="library-tag-filter"
                  class="input"
                  type="search"
                  autocomplete="off"
                  bind:value={tagFilter}
                />
              </div>
              <p class="field-hint">Entries must have every selected tag.</p>
              <fieldset class="tagpicker__list">
                <legend class="visually-hidden">Tags</legend>
                {#each tagOptions as item (item.name)}
                  <label class="check">
                    <input
                      type="checkbox"
                      checked={filters.tags.includes(item.name)}
                      onchange={(event) => toggleTag(item.name, event.currentTarget.checked)}
                    />
                    <span class="mono">{item.name}</span>
                    {#if item.count !== null}
                      <span class="faint small">{formatNumber(item.count)}</span>
                    {/if}
                  </label>
                {:else}
                  <p class="muted">No tag matches &ldquo;{tagFilter}&rdquo;.</p>
                {/each}
              </fieldset>
            {/if}
          </div>
        </div>
      </details>

      <label class="check">
        <input
          type="checkbox"
          checked={filters.mine}
          onchange={(event) => apply({ mine: event.currentTarget.checked })}
        />
        Only my entries
      </label>

      {#if filters.tags.length > 0}
        <ul class="chips" aria-label="Selected tags">
          {#each filters.tags as tag (tag)}
            <li>
              <button
                type="button"
                class="chip chip-button"
                aria-label="Remove tag {tag}"
                onclick={() => toggleTag(tag, false)}
              >
                {tag}
                <Icon name="x" size={12} />
              </button>
            </li>
          {/each}
        </ul>
      {/if}

      <span class="grow"></span>
      {#if activeCount > 0}
        <button type="button" class="btn btn-quiet" onclick={clearFilters}>Clear filters</button>
      {/if}
    </div>
  </div>
</form>

<section class="library-results" aria-labelledby="library-results-title">
  <h2 id="library-results-title" class="visually-hidden">Results</h2>
  <p class="results-summary muted" id="library-results" tabindex="-1" aria-live="polite">
    {#if results.loaded && results.data}
      {formatNumber(results.data.total)}
      {results.data.total === 1 ? 'entry' : 'entries'}{activeCount > 0 ? ' match your filters' : ''}
    {:else if results.error}
      The results could not be loaded.
    {:else}
      Loading entries&hellip;
    {/if}
    {#if results.loading && results.loaded}
      <span class="spinner" aria-hidden="true"></span>
    {/if}
  </p>

  {#if results.error && !results.loaded}
    <div class="card">
      <ErrorState error={results.error} onretry={results.reload} />
    </div>
  {:else if !results.loaded}
    <ul class="entry-list card" aria-busy="true">
      {#each [0, 1, 2, 3, 4] as row (row)}
        <li class="entry-row">
          <span class="skeleton entry-row__skeleton"></span>
          <span class="skeleton"></span>
        </li>
      {/each}
    </ul>
  {:else if results.data && results.data.items.length === 0}
    <div class="card">
      {#if activeCount > 0}
        <EmptyState
          icon="search"
          title="No entries match these filters"
          text="Try a different word, remove a tag, or clear the filters to see everything you can access."
        >
          <button type="button" class="btn btn-primary" onclick={clearFilters}>Clear filters</button>
        </EmptyState>
      {:else}
        <EmptyState
          title="The library is empty"
          text="Nothing has been published yet. Upload a package to add the first skill or grounding entry."
        >
          <a class="btn btn-primary" href={hrefFor(paths.upload())}>Upload package</a>
        </EmptyState>
      {/if}
    </div>
  {:else if results.data}
    {#if results.error}
      <p class="field-error" role="alert">
        The list could not be refreshed.
        <button type="button" class="btn btn-sm" onclick={() => results.reload()}>Try again</button>
      </p>
    {/if}
    <ul class={['entry-list', 'card', results.loading && 'entry-list-busy']} aria-busy={results.loading}>
      {#each results.data.items as entry (entry.id)}
        <li class="entry-row">
          <div class="entry-row__main">
            <h3 class="entry-row__title">
              <a href={entryHref(entry.name)}>{entry.display_name}</a>
            </h3>
            {#if entry.display_name !== entry.name}
              <span class="mono muted small wrap-anywhere">{entry.name}</span>
            {/if}
            <p class="entry-row__description">{entry.description}</p>
            {#if entry.tags.length > 0}
              <ul class="chips" aria-label="Tags">
                {#each entry.tags as tag (tag)}
                  <li class="chip">{tag}</li>
                {/each}
              </ul>
            {/if}
          </div>
          <div class="entry-row__meta">
            <div class="cluster">
              <KindBadge kind={entry.kind} />
              {#if entry.status !== 'published'}<StatusBadge status={entry.status} />{/if}
            </div>
            <span class="small muted">
              {#if entry.latest_version}
                Version <span class="mono">{entry.latest_version}</span>
              {:else}
                No published version
              {/if}
            </span>
            {#if entry.category}
              <span class="small muted">Category <span class="mono">{entry.category}</span></span>
            {/if}
            <span class="small muted">
              Updated <Time value={entry.updated_at} mode="relative" />
            </span>
          </div>
        </li>
      {/each}
    </ul>
    <Pagination
      page={filters.page}
      {pageCount}
      {total}
      pageSize={PAGE_SIZE}
      onpage={goToPage}
    />
  {/if}
</section>
