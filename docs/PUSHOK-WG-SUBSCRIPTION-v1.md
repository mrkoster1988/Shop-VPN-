# Pushok WireGuard subscription — interoperability contract v1 (draft)

Status: **design contract, NOT deployed or implemented**. Applies to Shop (issuer) and Pushok Flow Android (consumer). Existing VLESS feeds and standalone WireGuard .conf / QR import remain unchanged.

## Purpose and boundaries

Shop owns the subscription lifecycle, device ownership, payment state, expiry and WGDashboard peer operations. Flow reads a **device-scoped** subscription and establishes the WireGuard tunnel directly to the VPN node; traffic never traverses Shop. One device identity = one WireGuard peer. Revoking a download token is **not** equivalent to revoking an already installed WireGuard peer.

## Enrollment (proposed)

1. Authenticated Shop user chooses an existing subscription and device. Shop ensures a unique server-side device/peer identity; retries are idempotent.
2. User presses **Open in Flow**. Shop issues a random single-use, short-lived pairing code (e.g. 5 minutes), bound to subscription, device and account. The deep link contains **only the pairing code**; no WG configuration, API key, private key, long-lived credential, or subscription token. Verify the deep-link origin/association before claiming it is app-bound. Never silently import without confirmation.
3. Flow exchanges the code once over HTTPS at `POST /api/v1/flow/wg/pair`, request `{"code":"..."}`. Shop returns an opaque device-scoped access token and nonsecret metadata. The token is stored in Android Keystore-backed encrypted storage and sent using `Authorization: Bearer ...` over HTTPS.
4. Flow reads `GET /api/v1/flow/wg/subscription` with that token. Shop validates device, account, current status and revocation on **every** request. No token in URL, query string, analytics, crash reports, or referrer.
5. Revoking a device invalidates the token **and** disables/removes the matching peer at WGDashboard. If panel removal is pending, backend must NOT falsely report a completed revocation.

Exact API routes are proposed namespaces and must be implemented/tested in Shop before enabling Flow UI. Access tokens here are distinct from MTProxy Flow's existing `profile:read` / `wallet:read` session; that session alone cannot issue or revoke WireGuard access.

## Read response (JSON)

HTTP 200 with `Content-Type: application/vnd.pushok.wg-subscription+json;version=1`, `Cache-Control: no-store`, and `Referrer-Policy: no-referrer`.

```json
{
  "schema": "pushok.wg.subscription",
  "version": 1,
  "subscription_id": "a33b12ae-baa9-40a9-a871-cf84dc7b6e51",
  "device_id": "e06ec9f2-3183-4083-b915-1a05b5f48103",
  "title": "Pushok",
  "status": "active",
  "expires_at": "2027-01-15T12:00:00Z",
  "revision": 4,
  "profiles": [
    {
      "id": "wg-node-01",
      "name": "Основной сервер",
      "protocol": "wireguard",
      "config": "[Interface]\\nPrivateKey = <redacted>\\nAddress = 10.0.0.2/32\\n[Peer]\\nPublicKey = <redacted>\\nEndpoint = vpn.example:51820\\nAllowedIPs = 0.0.0.0/0\\n"
    }
  ]
}
```

The example configuration is **illustrative and deliberately unusable**. Real credentials must never appear in documentation, sample logs or tests.

Required root fields: `schema` (exact literal), `version` (integer 1), `subscription_id`, `device_id`, `title`, `status`, `expires_at`, `revision`, `profiles`. Required profile fields: stable `id`, `name`, `protocol` (`wireguard`), `config` (single strict WG client configuration). `expires_at` is an RFC 3339 UTC timestamp or null for explicitly unlimited access. `revision` is a monotonically increasing integer per device. Profile IDs must remain stable when server endpoints change.

Allowed statuses: `active`, `expired`, `revoked`, `pending`. For `active`, at least one valid profile is required. For other states, `profiles` must be empty; no freshly issued config can be returned. Expired and revoked states are authoritative only when delivered by the authenticated Shop endpoint (not on network errors). Back-end peer expiry/revocation is independently enforced at the WireGuard node; hiding profiles in JSON cannot revoke already installed peers.

## Failure and refresh behavior

- `401`: token invalid/expired; ask user to pair again without deleting already installed profiles automatically.
- `403`: device access blocked; display reason without exposing credentials.
- `404`: unknown/removed device; never create a replacement peer implicitly.
- `409`: provisioning or revocation still pending; retry with bounded backoff.
- `429`: rate-limited; honor `Retry-After`.
- `5xx` / timeout / bad JSON: retain the last validated local configuration, label it stale, never silently replace or clear it.
- `active` with a newer revision: fully validate all profiles first, then atomically replace the **matching device subscription** only; preserve selected stable profile ID where present.
- `expired` or `revoked`: mark access unavailable and disconnect only the matching managed tunnel; keep a safe UI record, but never enable it using cached settings. Server-side peer enforcement is still mandatory.
- An old revision must never overwrite a newer one. Duplicate stable profile IDs, duplicate WireGuard peer identities, inconsistent device/subscription IDs, malformed timestamps and unsupported schema versions must be rejected without changing local state.

## Validation and privacy gates

- HTTPS only (no redirects to another origin, no credential-bearing redirect), certificate verified, finite network timeouts, response size/profile count/config length capped by Flow's existing limits or stricter agreed limits.
- Strict UTF-8 and JSON parsing; reject unknown protocol values and malformed WireGuard configs, including executable `PreUp`, `PostUp`, `PreDown`, `PostDown` directives. Do not download nested URLs or remote imports from the document.
- `config` and bearer token are secrets: no telemetry, previews, notification text or backups. Credentials need encrypted storage with explicit logout/revocation policy.
- Shop remains the only holder of the WGDashboard administrative API key. Never expose the panel to Flow.
- Device-level idempotency, one peer per device, server-side expiry job + Shop worker reconciliation; test with Shop stopped and WGDashboard running.
- Distinguish server-authenticated terminal states from offline errors. Local clock is for display and conservative warnings, never sole authority for peer removal.
- Tests must include valid/invalid feed, replayed pairing code, concurrent pairing, failed panel provisioning, revision rollback, malformed configs, renewal, expiry, revocation, interrupted refresh, Android process restart, and unaffected VLESS subscriptions.

## Rollout order

1. Contract documentation in both repositories (**this change**).
2. Shop endpoint + authenticated single-use pairing + issuer tests (no public release until verified).
3. Flow parser, encrypted device token, atomic profile updates and tests.
4. Physical Android acceptance and real WGDashboard end-to-end acceptance: issue → import → connect → renew → expire → reactivate → revoke.
5. Only then enable "Open in Flow" for WireGuard customers. Gradually migrate account provisioning to Pushok ID later.

No existing routes, schemas, subscription feeds or released apps are changed by this document.
