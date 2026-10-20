<script>
  import { formatNumber } from '../format.js'
  import { pageWindow } from '../library-filters.js'

  /** @type {{ page: number, pageCount: number, total: number, pageSize: number, onpage: (page: number) => void }} */
  let { page, pageCount, total, pageSize, onpage } = $props()

  const first = $derived(total === 0 ? 0 : (page - 1) * pageSize + 1)
  const last = $derived(Math.min(total, page * pageSize))
  const items = $derived(pageWindow(page, pageCount))
</script>

{#if total > 0}
  <div class="pagination">
    <p class="muted">
      Showing {formatNumber(first)}–{formatNumber(last)} of {formatNumber(total)}
    </p>
    {#if pageCount > 1}
      <nav aria-label="Pagination">
        <ul>
          <li>
            <button
              type="button"
              class="page-link"
              disabled={page <= 1}
              onclick={() => onpage(page - 1)}
            >
              Previous
            </button>
          </li>
          {#each items as item, position (item === 'gap' ? `gap-${position}` : item)}
            <li>
              {#if item === 'gap'}
                <span class="page-gap" aria-hidden="true">…</span>
              {:else}
                <button
                  type="button"
                  class="page-link"
                  aria-label="Page {item}"
                  aria-current={item === page ? 'page' : undefined}
                  onclick={() => onpage(item)}
                >
                  {item}
                </button>
              {/if}
            </li>
          {/each}
          <li>
            <button
              type="button"
              class="page-link"
              disabled={page >= pageCount}
              onclick={() => onpage(page + 1)}
            >
              Next
            </button>
          </li>
        </ul>
      </nav>
    {/if}
  </div>
{/if}
