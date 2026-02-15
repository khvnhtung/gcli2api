#!/usr/bin/env python3
"""
Intercept and dump gcli2api outbound requests.

Usage:
    # Dump all outbound Antigravity requests to a JSON file:
    GCLI2API_DUMP_REQUESTS=1 python web.py

    # Or run this script standalone to see what UA/headers would be sent:
    python scripts/intercept_requests.py

This script has two modes:
1. Standalone: Shows current UA strings and simulates what headers would be sent
2. Monkey-patch: When GCLI2API_DUMP_REQUESTS=1 is set, patches httpx to log all requests
"""

import sys
import os
import json
import platform
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def show_current_ua_strings():
    """Display all UA strings gcli2api currently generates."""
    from src.utils import (
        ANTIGRAVITY_USER_AGENT,
        GEMINICLI_USER_AGENT,
        FALLBACK_VERSION,
        _cached_antigravity_version,
    )

    print("=" * 70)
    print("gcli2api User-Agent Audit")
    print("=" * 70)

    print(f"\n--- Antigravity UA ---")
    print(f"  String:   {ANTIGRAVITY_USER_AGENT}")
    print(f"  Version:  {_cached_antigravity_version or FALLBACK_VERSION}")
    print(f"  Fallback: {FALLBACK_VERSION}")
    print(f"  Platform: {platform.system().lower()}/{platform.machine().lower()}")

    print(f"\n--- GeminiCLI UA ---")
    print(f"  String:   {GEMINICLI_USER_AGENT}")

    print(f"\n--- Expected (Real Antigravity Client) ---")
    print(f"  Format:   antigravity/<version> <os>/<arch>")
    print(f"  Example:  antigravity/1.107.0 linux/x64")
    print(f"  Note:     Real client uses 'x64' not 'x86_64'")

    # Check mismatches
    print(f"\n--- Mismatches ---")
    issues = []

    version = _cached_antigravity_version or FALLBACK_VERSION
    if version == FALLBACK_VERSION and _cached_antigravity_version is None:
        issues.append(f"  [HIGH] Dynamic fetch failed, using fallback version {FALLBACK_VERSION}")

    if "x86_64" in ANTIGRAVITY_USER_AGENT:
        issues.append(f"  [MED]  Arch 'x86_64' should be 'x64' (real client uses 'x64')")

    if "Windows" in GEMINICLI_USER_AGENT and platform.system() != "Windows":
        issues.append(f"  [MED]  GeminiCLI UA claims Windows but running on {platform.system()}")

    if not issues:
        print("  None detected!")
    else:
        for issue in issues:
            print(issue)


def show_antigravity_headers():
    """Show exactly what headers an Antigravity request would send."""
    from src.utils import ANTIGRAVITY_USER_AGENT
    from src.api.antigravity import build_antigravity_headers

    print(f"\n{'=' * 70}")
    print("Simulated Antigravity Request Headers")
    print("=" * 70)

    headers = build_antigravity_headers(
        access_token="<REDACTED>",
        model_name="claude-sonnet-4-thinking"
    )
    for k, v in headers.items():
        display_v = v if k != "Authorization" else "Bearer <REDACTED>"
        print(f"  {k}: {display_v}")


def show_version_fetch_debug():
    """Debug the dynamic version fetching."""
    import requests

    print(f"\n{'=' * 70}")
    print("Version Fetch Debug")
    print("=" * 70)

    from src.utils import VERSION_BASE_URL, _get_updater_platform

    plat = _get_updater_platform()
    url = f"{VERSION_BASE_URL}/api/update/{plat}/stable/0.0.1"
    print(f"\n  [Updater API] {url}")
    try:
        resp = requests.get(url, timeout=5, allow_redirects=True)
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    productVersion: {data.get('productVersion', 'N/A')}")
            print(f"    ideVersion: {data.get('ideVersion', 'N/A')}")
            print(f"    name: {data.get('name', 'N/A')}")
        else:
            print(f"    Response: {resp.text[:300]!r}")
    except Exception as e:
        print(f"    Error: {e}")


def setup_request_dumper():
    """
    Monkey-patch httpx to dump all outbound requests to a log file.
    Call this before starting the server.

    Returns the path to the dump file.
    """
    import httpx

    dump_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(dump_dir, exist_ok=True)
    dump_file = os.path.join(dump_dir, f"request_dump_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")

    _original_send = httpx.AsyncClient.send

    async def _patched_send(self, request: httpx.Request, **kwargs):
        # Only dump requests to Google APIs
        host = request.url.host or ""
        if "googleapis.com" in host or "google.com" in host:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "http_version": getattr(self, '_http2', None),
            }

            # Try to capture body (may be stream)
            if request.content:
                try:
                    body_text = request.content.decode("utf-8")
                    # Try to parse as JSON for pretty printing
                    try:
                        body_json = json.loads(body_text)
                        # Redact access tokens
                        entry["body"] = body_json
                    except json.JSONDecodeError:
                        entry["body_raw"] = body_text[:5000]
                except Exception:
                    entry["body_raw"] = "<binary or unreadable>"

            with open(dump_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")

        return await _original_send(self, request, **kwargs)

    httpx.AsyncClient.send = _patched_send
    return dump_file


if __name__ == "__main__":
    show_current_ua_strings()
    show_antigravity_headers()
    show_version_fetch_debug()

    print(f"\n{'=' * 70}")
    print("Live Request Dumping")
    print("=" * 70)
    print("""
  To capture actual outbound requests, set the env var and restart:

    GCLI2API_DUMP_REQUESTS=1 systemctl --user restart gcli2api

  Or run manually:

    GCLI2API_DUMP_REQUESTS=1 python web.py

  Requests will be dumped to: logs/request_dump_<timestamp>.jsonl

  Then trigger a request (e.g. from OpenCode) and inspect:

    tail -f logs/request_dump_*.jsonl | python -m json.tool

  To stop dumping, restart without the env var:

    systemctl --user restart gcli2api
""")
