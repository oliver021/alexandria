# Alexandria Admin

Administration console for the Alexandria registry. A Svelte 5 single-page app (Vite 8) that the FastAPI process serves at `/admin/` from `admin/dist`. It talks to the REST API under `/api/v1` and has no runtime dependency besides Svelte.

## Commands

Requires Node 22.13 or newer.

```bash
npm install
npm run dev      # http://localhost:5173/admin/, proxies /api and /openapi.json to http://localhost:8000
npm run build    # writes admin/dist, which FastAPI serves at /admin/
npm test         # Vitest: jsdom + Testing Library, plus a real Chrome for layout and scrolling
npm run check    # svelte-check, fails on warnings
npm run check:hosting -- http://localhost:8000   # how a running server hosts the console (see "Hosting")
```

Set `ALEXANDRIA_API` to proxy `npm run dev` to another backend, for example `ALEXANDRIA_API=http://localhost:8741 npm run dev`.

### Tests

`npm test` runs every `*.test.js` file of `src/` in jsdom, and those of `browser/` and `scripts/` in plain Node (their first line says `@vitest-environment node`):

- `browser/console.test.js` builds the console, hosts it the way the registry has to (`hosting.js`), drives a headless Chrome over the DevTools pipe (`browser/chrome.js`, no extra package) and answers the calls of the console with canned data. It checks what jsdom cannot see: the layout of the Metadata card with keys chosen by a package, and that an error is on the screen, and has the focus, after a submit at several window sizes.
- It skips when no Chrome is installed (it looks for `google-chrome`, `chromium` and the macOS app; `CHROME_BIN` points to another one) or when `ALEXANDRIA_NO_BROWSER` is set. It fails when Chrome is there but does not start. Linux and macOS only.
- `scripts/hosting.test.js` tests `hosting.js` and `check-hosting.mjs`.

## Pages

Hash routes, so the server needs no fallback rule:

| Route | Page |
|---|---|
| `#/login` | Sign in. Accepts `?next=` with an in-app path only. |
| `#/` | Dashboard: totals by kind and status, latest entries, quick access, registry status. |
| `#/library` | Search with `q`, `kind`, `category`, repeatable `tag`, `status`, `mine`, `sort` and `page`, all kept in the address. |
| `#/library/<name>?tab=summary\|versions\|files\|use` | Entry. `version` and `file` select what the Files and Use tabs show. |
| `#/upload` | Drag and drop or choose a `.zip`, optional version, publish switch, real upload progress. |
| `#/tokens` | List, create and revoke API tokens. The secret is shown once, in memory only. |
| `#/users` | Admin only: list and create users, and deactivate them when the registry offers it (see "User deactivation"). |

## Layout

```
src/
  main.js, App.svelte        mount (not inside a frame), route guard, focus and title handling
  app.css, styles/           design tokens (light and dark), base, layout, components, pages
  routes/                    one component per page, entry/ holds the tabs of the entry page
  lib/
    api.js                   the only HTTP client (fetch, plus XMLHttpRequest for upload progress)
    auth.svelte.js           session (JWT and user) in sessionStorage
    router.svelte.js         hash router state; routes.js has the pure parts
    reasons.js               human explanations of package errors (contract section 1.6)
    filetree.js, format.js, snippets.js, library-filters.js, upload.js, paths.js, ...
    components/              shared components (dialog, tabs, tree, toasts, ...)
  test/                      Vitest setup and helpers
hosting.js                   what the console needs from the server that hosts it (see "Hosting")
csp.js                       the Content Security Policy that the build embeds
browser/                     test server and Chrome driver for the browser tests
scripts/check-hosting.mjs    checks a running server against hosting.js
```

## API client

`src/lib/api.js` adds `Authorization: Bearer`, normalizes every failure to `ApiError {status, code, message, reason}` from the `{"error": {...}}` body, ends the session and returns to the login page on a 401, and never parses a body for `204` responses or downloads. Downloads go through `fetch` to a `Blob`, so the credential only travels in a header, and the SHA-256 of the package is checked before it is saved.

### User deactivation

Deactivating a user is optional for a registry, so the console does not hard-code a route for it. When the Users page opens it reads `/openapi.json` once (`api.checkUserDeactivation()`) and looks for a `POST`, `PUT`, `PATCH` or `DELETE` under `/api/v1/users/{id}` whose path, `operationId` or summary says "deactivate".

| Result | What the page does |
|---|---|
| `available` | Shows the Actions column, the Deactivate buttons, the confirmation dialog and the bullets that describe deactivation. |
| `missing` | The document was read and has no such route. Shows no action and says the registry cannot do it. |
| `unknown` | The document could not be read. Shows no action and asks to reload. A failed read is not remembered, so the next visit tries again. |

Nothing is guessed: an unreadable document never turns into a call to a made-up route. A registry that adds the route gets the buttons without any change here.

## Hosting

`npm run build` writes a static site to `dist/`, which FastAPI mounts at `/admin/`. The session lives in the page, so whatever serves `dist/` has to send these headers. A `<meta>` tag cannot carry most of them. They are written down once, in `hosting.js`, which the check, the tests and this table follow.

| Path | Header | Why |
|---|---|---|
| `/admin`, `/admin/`, `/admin/index.html` and anything else under `/admin/` except `/admin/assets/*`, including a `404` | `Cache-Control: no-cache` | The page names the hashed files of one build and the next build deletes them (`emptyOutDir`). A copy cached without revalidation points at files that no longer exist and the console does not start. Without a `Cache-Control` a browser decides for itself and typically keeps the page for a tenth of the time since the file was last modified, so a returning visitor can stay on the old build for days. |
| `/admin/assets/*`, on `200` and `304` responses only | `Cache-Control: public, max-age=31536000, immutable` | File names carry a content hash, so they never change. A cached `404` would pin a missing file for a year. |
| every `/admin/*` response, redirects and errors included | `X-Frame-Options: DENY` | Stops other sites from framing the console. |
| every `/admin/*` response | `Content-Security-Policy`: the policy of `csp.js` plus `frame-ancestors 'none'` | The build already embeds `csp.js` as a `<meta>` tag, but browsers ignore `frame-ancestors` there. |
| every `/admin/*` response | `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` | `Referrer-Policy` is also set by a `<meta>` tag; `nosniff` has no `<meta>` equivalent. |

These headers are for `/admin` only: `/registry` sets its own, and the API needs none of them.

Without these headers the console still starts, but other pages can frame it and, after a new build, a cached copy of the page can point at files that no longer exist. As a second line of defence against framing, `main.js` shows only a message instead of the console when the page is not the top window of its tab (`src/lib/framing.js`). That check runs in the page, so it never replaces `X-Frame-Options` and `frame-ancestors`.

Building needs Node 22.13 or newer (`engines` in `package.json`).

### Check a deployment

```bash
npm run check:hosting                                   # http://localhost:8000
npm run check:hosting -- https://registry.example.test
```

It requests `/admin`, `/admin/`, `/admin/index.html`, every hashed file that the page names (and, when it has an `ETag`, again as a conditional request, which must answer `304`), a file that does not exist and `/api/v1/health`. It compares each answer with `hosting.js`, prints what differs and exits with `1` when anything does. Run it after every deploy, and after changing the server in front of the registry.

### FastAPI

A plain `StaticFiles` mount sends only `ETag` and `Last-Modified`, so the headers have to come from a middleware. This one next to the mount in `app/main.py` satisfies `npm run check:hosting`:

```python
ADMIN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
ADMIN_IMMUTABLE = "public, max-age=31536000, immutable"


@app.middleware("http")
async def admin_console_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/admin" or path.startswith("/admin/"):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = ADMIN_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        immutable = path.startswith("/admin/assets/") and response.status_code in (200, 304)
        response.headers["Cache-Control"] = ADMIN_IMMUTABLE if immutable else "no-cache"
    return response
```

`ADMIN_CSP` must be the directives of `csp.js` followed by `frame-ancestors 'none'`. A backend test that reads `admin/csp.js` and compares keeps the two from drifting, and one that requests the paths listed above keeps the middleware honest.

## Security rules

- Everything that comes from the server or from a package is rendered as text. `{@html}`, `innerHTML`, `eval` and `javascript:` links are forbidden, and `src/security.test.js` enforces it.
- The session lives in `sessionStorage`, never in `localStorage`. Nothing secret goes in a URL.
- Path parameters are encoded with `encodeURIComponent` (file paths per segment, rejecting `..`).
- The build injects a strict Content Security Policy (`csp.js`): same origin only, no inline scripts, no inline styles. The dev server does not apply it. `csp.js` holds only directives that a `<meta>` tag honours, which is why `frame-ancestors` is not in it (see Hosting).
- The console never runs inside a frame: `main.js` mounts nothing and makes no request when `window.top` is not the page itself.

## Conventions

- A message about a form that is drawn above or inside the form (a failed upload, a refused user, a rejected field) is easy to miss: after a submit the page can be scrolled so that it sits above the window. Give the alert `id` and `focusable` and call `focusAndReveal(id)` from `lib/focus.js`; for fields, call `focusFirstInvalid(form)`. Both move the focus and scroll to the middle of the window, and `layout.css` keeps the sticky header from covering what they scroll to. Messages inside a dialog or a toast need none of this.
- Svelte 5 runes only: no `export let`, `on:` directives, stores or `<slot>`.
- No external resources: system fonts, inline SVG icons, no CDN.
- Interface text is English and mirrors the messages of the API.
- Colors are defined in `src/styles/tokens.css` and meet WCAG AA in light and dark mode.
