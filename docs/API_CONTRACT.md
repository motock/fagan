# The dashboard API contract

This page describes the HTTP contract the dashboard publishes, how it is
versioned, how a replacement UI authenticates against it, and the two
deployment shapes that are supported. It is written for an operator or a UI
author who does not want to read `app/dashboard.py`.

## What the contract is

The contract is the OpenAPI 3 document the dashboard generates from its own
routes. There are three copies of it, and they are the same bytes:

- **Live, authenticated:** served at `/api/openapi.json`, behind the same
  authentication as every other `/api/` route.
- **Committed:** checked into the repository at `docs/api/openapi.json`.
- **Checked in CI:** a test compares the live application against the
  committed snapshot, so a route or schema change that did not update the
  snapshot fails the build.

The spec lives under `/api/` on purpose. FastAPI's default documentation
endpoints (`/openapi.json`, `/docs`, `/redoc`) are installed by the application
object and therefore sit *outside* the `/api/` credential gate. `/docs` and
`/redoc` are off by default and the spec is served at `/api/openapi.json` so it
is protected by construction. Public documentation is an explicit operator
opt-in — see `PIPELINE_DASHBOARD_PUBLIC_DOCS` in
[REFERENCE.md](../REFERENCE.md).

## Versioning policy

`API_VERSION` in `app/api_contract.py` is a semantic version, and it is
stamped into `info.version` of the served spec. It tracks the HTTP surface, not
the release of the dashboard:

- **MAJOR** bumps only for a **breaking** change.
- **MINOR** bumps for an **additive** change.
- A breaking change also requires a `CHANGELOG.md` entry describing it and the
  migration path.

`scripts/api_contract.py classify` sorts each difference between the live app
and the committed snapshot into `breaking` or `additive`. The classifier rules
are, literally:

- **BREAKING** — a removed path or method, a removed response status, a
  property removed from a 2xx response schema, a property whose `type`
  changed, an enum value removed from a response, a request parameter or
  request-body property that became required or was added as required, and a
  request parameter the old spec declared that the new one dropped.
- **ADDITIVE** — everything else (new path, new optional parameter, new
  response property, new enum value, ...).

The classifier is a structural diff, not an OpenAPI type checker: it resolves
local `$ref`s and compares the resolved nodes as plain JSON. It does not model
`oneOf`/`allOf`/`anyOf` semantics, discriminators, formats, defaults,
pattern/format constraints, or nullability expressed through union types, and
it treats external references as opaque. A difference it cannot classify is
one a human has to read — the snapshot diff is the safety net.

### Regenerating the snapshot

When you intentionally change a route or a schema, regenerate the committed
snapshot and commit it alongside the code:

```console
python scripts/api_contract.py export
```

`export` rewrites `docs/api/openapi.json` from the live application in a
neutral environment (every `PIPELINE_*` and `LOCAL_AGENT_*` variable is
dropped while the app is imported), so the same commit produces the same bytes
on your machine and in CI. Routes that exist only under a particular operator
configuration — the OIDC `/auth/*` redirects — are therefore not in the
snapshot: the contract covers the surface every deployment serves.

A failing check means the live application and the committed snapshot
disagree. The diff it prints names the path, method and property that changed;
`classify` tells you whether the change was breaking. Update the snapshot if
the change was intended, and bump `API_VERSION` — MAJOR for breaking, MINOR for
additive.

## Authenticating a replacement UI

The dashboard supports two identity modes. Which one is active is set by the
operator (`PIPELINE_IDENTITY_PROVIDER`); a UI author must be prepared for the
deployment it is pointed at.

### Shared-secret mode

Send the dashboard shared secret in the `X-Pipeline-API-Key` header on every
`/api/` request. The secret is a 256-bit `token_urlsafe` value that the
dashboard generates on first use and persists in `.dashboard_api_key` at the
repository root, with mode `0600`. An operator reads it from that file (for
example `cat .dashboard_api_key`) and hands it to the UI out of band. The
dashboard's own bundled UI receives it as `window.__PIPELINE_API_KEY__`,
injected into the served `index.html`; that injection happens only when the
bundled UI is served, so a replacement UI has to obtain the secret from the
operator instead.

Never commit or log the key. A request with a missing or wrong key is refused
with `401`.

### OIDC mode

When OIDC is configured, the `/api/` gate accepts a bearer token:
`Authorization: Bearer <token>`. The token is validated against the configured
identity provider.

### The session cookie is same-origin only

In OIDC mode the browser session is a `__Host-pipeline_session` cookie. The
`__Host-` prefix makes the browser refuse a cookie that is not
`Secure`/`Path=/`/host-only, and it cannot be read by another origin. State-
changing requests that rely on the cookie must also prove same origin. The
cookie is therefore usable only by a UI served from the dashboard's own origin
— which is why same-origin hosting is the recommended shape below.

## The two supported deployment shapes

### SAME-ORIGIN (recommended)

Serve your replacement UI from the same origin as the API:

1. Set `PIPELINE_DASHBOARD_UI=off` so the dashboard is API-only. `/` and every
   static asset answer `404`, and the bundled UI's key injection never
   happens.
2. Have your reverse proxy serve your UI and forward `/api/` to the dashboard
   on the same host and port.
3. Keep the `__Host-` cookie model: the browser sees one origin, so the
   session cookie works and no CORS is involved.

### CROSS-ORIGIN

If the UI must live on a different origin, opt in explicitly:

1. Set `PIPELINE_DASHBOARD_CORS_ORIGINS` to the exact origins, comma-separated
   (at most 20 entries; plain `http` only for `localhost`/`127.0.0.1`,
   `https` everywhere else). Unset or blank means no CORS middleware is
   installed at all.
2. Use **header credentials only** — `X-Pipeline-API-Key` or a bearer token.
   The middleware is installed with `allow_credentials=False`, so a cookie-
   bearing request can never succeed from another origin. Do not expect the
   OIDC session cookie to work cross-origin.

Wildcards are rejected: the parser refuses any entry containing `*`, so list
each origin explicitly. A cross-origin request is answered with CORS headers
only for the origins you listed, and `X-Correlation-ID` is exposed so your
client can log it.

## Headers a UI must send

| Header | When | Value |
| --- | --- | --- |
| `X-Pipeline-API-Key` | Shared-secret mode, every `/api/` request | The dashboard shared secret. |
| `Authorization` | OIDC mode with a bearer token | `Bearer <token>`. |
| `X-Pipeline-Origin` | Routes that are UI-only | The literal `ui`. |
| `X-Correlation-ID` | Optional but recommended | An id of your choosing; it is echoed on the response, so it ties your log line to the server's. |

`X-Pipeline-Origin: ui` is mandatory on the UI-only routes. Those routes are:

- `GET /api/worktree/patch/{patch_id}` — review a stored patch.
- `POST /api/worktree/patch/{patch_id}/apply` — apply a stored patch.

Both are gated by `require_ui_origin` in `app/auth.py`: an absent header, the
value `chat`, and any unknown value are all refused with a generic `403
origin not permitted`. The chat model may *propose* a patch; only a
ui-origin-authenticated human may review and apply one.

## What is not yet part of the contract

Some behaviour is deliberately left out of the spec, so do not build a client
that depends on it:

- **Per-operation numeric bounds and pagination cursors.** Limits are not
  declared in the spec and cursors are not part of the contract.
- **Endpoints the spec marks free-form.** These operations have no typed 2xx
  response body and are exempted in `tests/unit/test_dashboard_response_schemas.py`
  with a reason:
  - `GET /api/worktree/patch/{patch_id}` — the exemption is pinned to the
    exact decorator text asserted by
    `tests/unit/test_patch_route_refusal_logging.py`, so it cannot be typed
    without editing a test that may not change.
  - The `/auth/` browser-login routes are outside the dashboard API surface
    and are skipped by prefix.
- **The SSE stream.** `POST /api/chat/stream` is a text event stream, not a
  JSON body, so it cannot be a `components/schemas` reference.
