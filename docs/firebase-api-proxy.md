# Firebase-authenticated API proxy design

## Status

Implementation design for issue #30.

## Context

The application's HTTP triggers use Azure Functions function-level
authorization. That is appropriate for direct service access, but a browser
application must not receive or retain an Azure Function key.

The front end already authenticates users with Firebase Authentication. A
single proxy entry point will accept the caller's Firebase ID token, validate
it for the configured Firebase project, and dispatch the request through a
fixed internal mapping. The existing HTTP triggers remain function-protected
for direct callers.

The proxy is an application gateway, not a general reverse proxy. Callers
select a documented public proxy path; they cannot supply a destination URL,
an Azure route, or a function key.

## Goals

- Provide Firebase-authenticated access to every existing HTTP API.
- Support both `GET` and `POST` proxy requests.
- Use readable paths such as `POST /api/proxy/ai-qa`.
- Validate Firebase ID tokens using the configured `FIREBASE_PROJECT_ID`.
- Dispatch only through a fixed, version-controlled target mapping.
- Preserve the mapped API's request and response contracts.
- Keep the basic diagnostic available without a Firebase token and enrich its
  response with verified claims when a valid token is supplied.
- Keep every existing direct HTTP trigger protected by function-level
  authorization.
- Keep authentication, target resolution, request adaptation, and dispatch
  independently testable.

## Non-goals

- Accepting arbitrary destination URLs or Azure Function routes
- Exposing Azure Function keys to the browser
- Accepting Firebase custom tokens, refresh tokens, or Google OAuth access
  tokens in place of Firebase ID tokens
- Checking Firebase token revocation or disabled-user state in the first
  version
- Firebase App Check validation in the first version
- Adding role- or claim-specific authorization rules
- Changing the behavior or response schema of a mapped API
- Proxying timer triggers or non-HTTP application operations

## Public routes and internal mapping

The proxy exposes only the following method and path combinations:

| Proxy request | Existing handler route |
| --- | --- |
| `GET /api/proxy/diag` | `GET /api/diag` |
| `POST /api/proxy/ai-qa-diagnostic` | `POST /api/diag/ai-qa` |
| `POST /api/proxy/ai-qa` | `POST /api/ai/qa` |
| `GET /api/proxy/earthquakes` | `GET /api/earthquakes` |
| `GET /api/proxy/earthquakes/{id}` | `GET /api/earthquakes/{id}` |
| `POST /api/proxy/earthquakes-acquire` | `POST /api/earthquakes/acquire` |

The method is part of the mapping. A path that is known for a different
method returns HTTP 405. A path that does not match a mapping returns HTTP 404.
Adding another proxied API requires an explicit mapping and tests.

The exact-earthquake path is the only initial mapping with a caller-supplied
path segment. The proxy treats `id` as data for that mapping and never
concatenates it into an arbitrary destination.

## Authentication

The caller supplies a Firebase Authentication ID token using the standard
header:

```http
Authorization: Bearer <firebase-id-token>
```

Except for `GET /api/proxy/diag`, the proxy accepts exactly one Bearer token. A
missing header, an unsupported authentication scheme, an empty token, or an
invalid token returns HTTP 401. Authentication happens before target dispatch
and before the request body is parsed by a mapped API.

The basic diagnostic is the only authentication exception. When its Bearer
token is absent, malformed, unavailable for verification, or invalid, the
proxy returns the existing diagnostic response unchanged. When its token is
successfully verified, the proxy adds the decoded verified claims under
`security.claims` and a separate, server-derived `security.role` value to the
diagnostic response. The raw encoded token is never returned. This exception
does not apply to `POST /api/proxy/ai-qa-diagnostic`.

`FIREBASE_PROJECT_ID` is the only required Firebase application setting. It
identifies the expected token audience and issuer. Validation must verify at
least:

- the Firebase signature against Google's published signing certificates;
- the `RS256` algorithm and a recognized signing-key ID;
- `aud` equals `FIREBASE_PROJECT_ID`;
- `iss` equals `https://securetoken.google.com/<FIREBASE_PROJECT_ID>`;
- `sub` is a non-empty Firebase user ID;
- `exp`, `iat`, and `auth_time` are valid for the current time.

Signing certificates are public and must be cached according to their HTTP
cache metadata. Basic validation therefore does not require a Firebase API
key, Firebase web-app configuration, or a service-account private key.

The first version does not make a per-request Firebase Admin API call to check
whether a token was revoked or its user was disabled. A cryptographically
valid token remains acceptable until it expires. Revocation checking can be
added later with an explicitly provisioned server credential if product risk
requires it.

Firebase ID tokens do not supply the application's role. The proxy derives it
from the verified token's email and the optional comma-separated
`ADMIN_USER_EMAILS` setting. Email comparison is trimmed and
case-insensitive. A user receives `admin` when the email is listed; every other
verified user receives `user`. The decoded claims remain unchanged, and the
server-derived `security.role` is authoritative even if the token contains a
custom claim with the same name. The first version reports the role but does
not restrict individual proxy targets by role.

If `FIREBASE_PROJECT_ID` is missing or signing certificates cannot be obtained
or refreshed when required, protected proxy targets fail closed with HTTP 503.
The basic diagnostic instead follows its optional-authentication behavior and
returns its unchanged response without `security`. Authentication
responses and logs must never contain the token.

## Azure Functions authorization boundary

The proxy trigger uses `AuthLevel.ANONYMOUS` so the Azure Functions host allows
the Firebase Bearer token to reach application validation without requiring a
browser-visible Function key. This is a documented exception to the
repository's function-level authorization default; the endpoint is not
anonymous at the application layer.

All mapped HTTP triggers retain `AuthLevel.FUNCTION`. Their direct URLs still
require an Azure Function key and do not begin accepting Firebase tokens.

## Dispatch

The initial implementation dispatches to mapped application handlers within
the same Function App. It does not call the Function App's public HTTP URL.
This avoids an additional network invocation, Function-key injection, and a
second platform timeout while preserving the same handler behavior.

The dispatcher adapts the authenticated proxy request to the mapped handler's
expected `HttpRequest` shape. It supplies only target-specific route
parameters and removes proxy-only routing state. The Firebase Authorization
header is not required by a mapped handler and is not propagated as an
internal credential.

Calling a mapped handler directly intentionally bypasses the Azure Functions
host's function-key check only after Firebase validation succeeds. External
calls to the handler's original route continue through the host and remain
function-protected.

## Request forwarding

### GET

The dispatcher preserves the mapped request's query parameters. For example:

```http
GET /api/proxy/earthquakes?min_magnitude=4&format=json
```

is processed by the earthquake collection handler with `min_magnitude=4` and
`format=json`. The target is selected from the path, so there is no proxy
selector to remove from the query string.

For `GET /api/proxy/earthquakes/{id}`, the dispatcher supplies `id` as the
mapped handler's route parameter and preserves supported query parameters such
as `format`.

### POST

The dispatcher preserves the request body bytes, content type, and applicable
query parameters. It does not deserialize and reserialize mapped JSON bodies.
The mapped handler remains responsible for body parsing, schema validation,
and its existing HTTP 400 responses.

The proxy initially accepts only `GET` and `POST`. Browser CORS preflight is
handled separately and is never dispatched to a mapped API.

## Response behavior

After successful authentication and dispatch, the proxy returns the mapped
handler's `HttpResponse` without wrapping or rewriting it. This preserves:

- status code;
- body bytes;
- MIME type and character set;
- mapped application response headers.

Authentication, target-resolution, and method errors are owned by the proxy.
Once a mapped handler runs, its success and error responses pass through as
returned. The sole intentional response change is the addition of
`security` to a successful basic diagnostic response when the supplied token
was verified.

## Error behavior

| Condition | Status |
| --- | --- |
| Missing, malformed, expired, or invalid Firebase ID token | `401` |
| Missing or invalid token for `GET /api/proxy/diag` | Existing diagnostic response |
| Unknown proxy target | `404` |
| Known proxy path used with an unsupported method | `405` |
| Missing Firebase project configuration | `503` |
| Required signing certificates temporarily unavailable | `503` |
| Mapped handler success or failure | Handler response unchanged |

Proxy-generated error bodies use a small JSON object with a generic message
and UTC timestamp. Token contents and low-level certificate or validation
errors remain server-side.

## CORS

The deployed Function App must allow the front end's configured origins,
methods, and the `Authorization` and `Content-Type` request headers. Production
must not use an unrestricted origin merely to make Firebase authentication
work. CORS controls browser access but is not an authentication mechanism.

## Configuration

`FIREBASE_PROJECT_ID` is required only when a protected proxy target is
invoked or the diagnostic receives a token to verify. A missing value must not
prevent unrelated timer triggers, direct function-protected APIs, or the
unauthenticated proxy diagnostic from running.

`ADMIN_USER_EMAILS` is optional. It contains comma-separated email
addresses; whitespace and case are ignored during matching. When it is empty
or absent, no verified user receives the `admin` role.

The setting name belongs in the tracked settings example and environment
availability diagnostics. Its deployed value is public project identity, not
a private credential. `local.settings.json` remains untracked.

## Validation

Automated tests cover:

- proxy route metadata, including the documented anonymous-auth exception;
- continued function-level authorization for every existing direct API;
- complete method/path mapping for the existing HTTP APIs;
- missing, malformed, expired, incorrectly issued, and incorrectly scoped ID
  tokens;
- valid-token dispatch and extracted Firebase user identity;
- failure closed when project configuration or signing keys are unavailable;
- no dispatch after an authentication failure;
- unchanged basic diagnostic responses for missing, malformed, invalid, or
  unverifiable optional tokens;
- decoded verified claims attached only to the basic diagnostic response;
- case-insensitive admin-email matching and an authoritative security role
  independent of any token-provided `role` claim;
- GET query and exact-record route-parameter adaptation;
- POST body, content-type, and query preservation;
- unknown paths and method mismatches;
- unchanged mapped success and error responses;
- absence of tokens and credentials from responses and logs.

Token verification is isolated behind an application interface so unit tests
do not depend on live Firebase authentication or certificate endpoints.

## References

- [Firebase: Verify ID tokens](https://firebase.google.com/docs/auth/admin/verify-id-tokens)
- [Firebase: Verify ID tokens using a third-party JWT library](https://firebase.google.com/docs/auth/admin/verify-id-tokens#verify_id_tokens_using_a_third-party_jwt_library)
- [Azure Functions HTTP trigger authorization levels](https://learn.microsoft.com/azure/azure-functions/functions-bindings-http-webhook-trigger#http-auth)
