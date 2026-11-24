#!/usr/bin/env python3
"""Read-only public-route readiness check for the MTProxy → Shop → Flow pilot.

No login, no provisioning, no bearer credentials, no insecure TLS option.
A successful public check does NOT prove that WGDashboard is configured.
"""
import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

ORIGIN = "https://pushok.shop"
CHECKS = (
    ("/api/v1/flow/capabilities", "mtproxy"),
    ("/shop/health", "shop"),
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(path, kind, opener):
    request = urllib.request.Request(
        ORIGIN + path,
        headers={"Accept": "application/json", "Cache-Control": "no-store"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=8) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            if "application/json" not in (response.headers.get("Content-Type") or "").lower():
                return False, "unexpected Content-Type"
            payload = response.read(4097)
            if len(payload) > 4096:
                return False, "oversized response"
            value = json.loads(payload)
            if not isinstance(value, dict):
                return False, "invalid JSON object"
            if kind == "mtproxy" and (value.get("protocol_version") != 1 or
                                      value.get("session_ttl_seconds") != 43200):
                return False, "unexpected MTProxy Flow protocol"
            if kind == "shop" and value.get("status") != "ok":
                return False, "unexpected Shop health response"
            return True, "HTTP 200, expected JSON"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Never log request headers, usernames, configs or response payloads.
        return False, type(exc).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mtproxy-only", action="store_true",
                        help="Check only the MTProxy public capability route")
    args = parser.parse_args()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect(),
    )
    checks = CHECKS[:1] if args.mtproxy_only else CHECKS
    passed = True
    for path, kind in checks:
        ok, detail = check(path, kind, opener)
        print(f"{'PASS' if ok else 'FAIL'} {kind} {path}: {detail}")
        passed = passed and ok
    print("INFO: This check cannot validate Shop trial policy, WGDashboard expiry, or Android traffic.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
