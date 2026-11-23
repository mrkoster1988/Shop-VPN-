# SOO Pushok: Shop → Flow pairing v1 (candidate)

Separate scoped Shop session, not Pushok ID SSO and not an extension of MTProxy access.
The client must be implemented and tested in Pushok Flow separately.

## Availability
Default OFF. Requires FLOW_SHOP_PAIRING_ENABLED=true and HTTPS PUBLIC_URL.
Check GET /api/flow/shop/capabilities. Do not activate production until Android
acceptance, explicit owner approval, and safe rollout. All endpoints use the Shop
base path (e.g. /shop). The existing Shop login and subscription-link APIs remain.

## Native client protocol
1. Generate a new cryptographically random 32-byte verifier, base64url without
   padding (43 ASCII characters). Persist it securely only during the request.
2. Compute challenge = base64url(SHA256(ASCII verifier)), no padding.
   POST /api/flow/shop/auth/start with {"challenge":"..."}.
   The response contains a 16-character lowercase hex public code, approval_url,
   expires_at (UTC Unix seconds), and poll_interval=5.
3. Show the code in the app; open approval_url in the browser / authorized Shop
   cabinet. The URL fragment prefills the code, but never authorizes silently.
   Existing Shop authentication and explicit confirmation are required.
4. POST /api/flow/shop/auth/exchange with {"code":"...","verifier":"..."}.
   Poll no faster than 5 seconds, stop at request expiry (300 seconds) or denial.
   Pending: {"state":"pending","retry_after":5}. Denied: terminal.
   Authorized: state, access_token (psf_...), token_type Bearer,
   scope wireguard:read, expires_at (up to 43200 seconds, capped by parent session).
   Lost response: same proof within request lifetime returns the same token
   without extending expiry. New explicit approval supersedes an older
   unexchanged approval. One active native read session per Shop account.
5. GET /api/flow/shop/connections with Authorization: Bearer <token>.
   Read only the user's WG subscription descriptors; pending is not ready.
   GET /api/flow/shop/subscriptions/{sid} with the same Authorization header
   returns existing pushok-flow-subscription v1 with stable revision.
   Never send the token to another origin or through query parameters.
6. POST /api/flow/shop/auth/logout with the bearer revokes the native session.
   Browser-side POST /api/flow/shop/auth/revoke revokes all linked requests
   for that Shop account. Browser logout/password change/reset also terminates
   linked native access. Restore clears the pairing ledger.
   The native client must clear local credentials on logout; 401 means reauthorize.

## Security and limitations
- Only hashes are stored for the public code and native token. Challenge
  verification uses constant-time comparison. Tokens never enter browser URLs.
- Bound auth rate limit, max 1000 pending/approved requests, expiry cleanup.
  Validation errors never echo the submitted proof.
- The capability, approval, and scoped reads do not create a purchase.
  Explicit approval may enqueue the existing starter grant if the owner enabled it.
- Authorization, scope and the feed snapshot are checked in the same transaction.
- No balance, order, admin, MTProxy, panel credentials or cross-account access.
- Revoking API access does not revoke an already downloaded WireGuard key.
  No claim of separate per-device peer in this version; native pairing is limited
  to one active read session. Client-side import must still be user-confirmed.
- Disabling feature gates stops new authorization and reads, not server peer traffic.
- Shop account identity remains separate until a verified Pushok ID integration.
  Do not use an MTProxy profile:read/wallet:read token for this interface.

## Acceptance
Python auth/ownership/expiry/replay tests, restore and upgrade tests, Node explicit
consent/logout tests. Full CI and real Android/HTTPS/WGDashboard acceptance are
required. No server rollout or release is implied by this candidate.
