#!/usr/bin/env python3
"""
Test upstream Antigravity API directly, bypassing gcli2api's local caps.

Usage:
    python scripts/test_upstream.py --test list-models
    python scripts/test_upstream.py --model claude-opus-4-6-thinking --test max-output-tokens --values 64000,128000
    python scripts/test_upstream.py --model gemini-2.5-flash --prompt "Say hello" --max-output-tokens 128000
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so we can import gcli2api modules for token refresh
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)


DB_PATH = PROJECT_ROOT / "creds" / "credentials.db"
DEFAULT_BASE_URL = "https://daily-cloudcode-pa.sandbox.googleapis.com"
USER_AGENT = "grpc-node/1.65.0 grpc-node-js/1.12.5"

SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "OFF"},
]


def get_credential() -> dict:
    """Extract a valid credential from the gcli2api database."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT filename, credential_data FROM antigravity_credentials "
            "WHERE disabled=0 ORDER BY last_success DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("ERROR: No enabled antigravity credentials found", file=sys.stderr)
            sys.exit(1)

        cred = json.loads(row["credential_data"])
        access_token = cred.get("access_token") or cred.get("token")
        if not access_token:
            print("ERROR: No access_token in credential", file=sys.stderr)
            sys.exit(1)

        expiry = cred.get("expiry", "")
        if expiry:
            try:
                exp_dt = datetime.fromisoformat(expiry)
                if exp_dt < datetime.now(timezone.utc):
                    print(
                        f"WARNING: Token expired at {expiry}. "
                        "Attempting refresh via gcli2api import...",
                        file=sys.stderr,
                    )
                    access_token = refresh_token_via_gcli2api(row["filename"])
                    if not access_token:
                        print(
                            "ERROR: Token refresh failed. Try making a request through "
                            "gcli2api first to trigger refresh, then re-run.",
                            file=sys.stderr,
                        )
                        sys.exit(1)
            except (ValueError, TypeError):
                pass

        return {
            "filename": row["filename"],
            "access_token": access_token,
            "project_id": cred.get("project_id", ""),
            "expiry": expiry,
        }
    finally:
        conn.close()


def refresh_token_via_gcli2api(filename: str) -> str | None:
    """Try to refresh the token using gcli2api's credential manager."""
    try:
        from src.credential_manager import credential_manager

        async def _refresh():
            await credential_manager._ensure_initialized()
            cred_result = await credential_manager.get_valid_credential(mode="antigravity")
            if cred_result:
                _, cred_data = cred_result
                return cred_data.get("access_token") or cred_data.get("token")
            return None

        return asyncio.run(_refresh())
    except Exception as e:
        print(f"WARNING: Could not refresh via gcli2api: {e}", file=sys.stderr)
        return None


def get_base_url() -> str:
    """Get the upstream API base URL from config or env."""
    import os

    url = os.environ.get("ANTIGRAVITY_API_URL", "")
    if url:
        return url.rstrip("/")

    # Try reading from gcli2api's config DB
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT value FROM config WHERE key='antigravity_api_url'"
            ).fetchone()
            if row:
                return row[0].rstrip("/")
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    return DEFAULT_BASE_URL


def build_headers(access_token: str) -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
        "requestId": f"req-{uuid.uuid4()}",
        "requestType": "agent",
    }


async def test_list_models(cred: dict, base_url: str):
    """Fetch available models from the upstream API."""
    url = f"{base_url}/v1internal:fetchAvailableModels"
    headers = build_headers(cred["access_token"])
    payload = {"project": cred["project_id"]}

    print(f"\n--- Fetching models from {url} ---\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        print(f"ERROR: HTTP {resp.status_code}")
        print(resp.text[:2000])
        return

    data = resp.json()
    print(json.dumps(data, indent=2)[:5000])

    # Extract model names and limits if available
    models = data.get("models", data.get("availableModels", []))
    if models:
        print(f"\n--- Found {len(models)} models ---\n")
        for m in models:
            name = m.get("name", m.get("model", "unknown"))
            limits = {
                k: v
                for k, v in m.items()
                if "token" in k.lower() or "limit" in k.lower() or "output" in k.lower()
            }
            print(f"  {name}: {limits if limits else '(no limit info)'}")


async def test_max_output_tokens(cred: dict, base_url: str, model: str, values: list[int]):
    """Test which maxOutputTokens values the upstream API accepts."""
    url = f"{base_url}/v1internal:generateContent"
    headers = build_headers(cred["access_token"])

    print(f"\n--- Testing maxOutputTokens for {model} ---\n")
    print(f"{'Value':>10} | {'Status':>6} | Result")
    print("-" * 60)

    for val in values:
        payload = {
            "model": model,
            "project": cred["project_id"],
            "request": {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "Reply with exactly: OK"}],
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": val,
                    "temperature": 1.0,
                    "topK": 64,
                },
                "safetySettings": SAFETY_SETTINGS,
            },
        }

        # Add thinking config for thinking models
        if "thinking" in model:
            payload["request"]["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": 1024
            }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                print(f"{val:>10} | {'OK':>6} | Success")
            else:
                body = resp.text[:200]
                print(f"{val:>10} | {resp.status_code:>6} | {body}")
        except Exception as e:
            print(f"{val:>10} | {'ERR':>6} | {e}")

        # Small delay between requests to avoid rate limiting
        await asyncio.sleep(1)


async def test_prompt(
    cred: dict,
    base_url: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    stream: bool = False,
):
    """Send a prompt directly to the upstream API."""
    if stream:
        url = f"{base_url}/v1internal:streamGenerateContent?alt=sse"
    else:
        url = f"{base_url}/v1internal:generateContent"

    headers = build_headers(cred["access_token"])

    payload = {
        "model": model,
        "project": cred["project_id"],
        "request": {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": 1.0,
                "topK": 64,
            },
            "safetySettings": SAFETY_SETTINGS,
        },
    }

    if "thinking" in model:
        payload["request"]["generationConfig"]["thinkingConfig"] = {
            "thinkingBudget": 1024
        }

    print(f"\n--- Sending prompt to {model} (maxOutputTokens={max_output_tokens}) ---")
    print(f"URL: {url}")
    print(f"Stream: {stream}\n")

    if stream:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(f"ERROR: HTTP {resp.status_code}")
                    print(body.decode()[:2000])
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip():
                            try:
                                parsed = json.loads(data)
                                # Extract text from candidates
                                for cand in parsed.get("candidates", []):
                                    for part in cand.get("content", {}).get("parts", []):
                                        if "text" in part:
                                            print(part["text"], end="", flush=True)
                            except json.JSONDecodeError:
                                pass
                print()  # Final newline
    else:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code}")
            print(resp.text[:2000])
            return

        data = resp.json()
        # Print usage metadata
        usage = data.get("usageMetadata", {})
        if usage:
            print(f"Usage: {json.dumps(usage, indent=2)}")

        # Print response text
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "text" in part:
                    print(f"\nResponse:\n{part['text']}")
                elif "thought" in part:
                    print(f"\n[Thinking]: {part['thought'][:200]}...")


async def main():
    parser = argparse.ArgumentParser(
        description="Test upstream Antigravity API directly"
    )
    parser.add_argument(
        "--test",
        choices=["list-models", "max-output-tokens", "prompt"],
        default="prompt",
        help="Test type to run",
    )
    parser.add_argument("--model", default="claude-opus-4-6-thinking", help="Model name")
    parser.add_argument("--prompt", default="Say hello in one sentence.", help="Prompt text")
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=64000,
        help="maxOutputTokens value",
    )
    parser.add_argument(
        "--values",
        help="Comma-separated maxOutputTokens values to test (for max-output-tokens test)",
    )
    parser.add_argument("--stream", action="store_true", help="Use streaming endpoint")
    parser.add_argument("--base-url", help="Override upstream base URL")

    args = parser.parse_args()

    cred = get_credential()
    base_url = args.base_url or get_base_url()

    print(f"Credential: {cred['filename']}")
    print(f"Project: {cred['project_id']}")
    print(f"Token expiry: {cred['expiry']}")
    print(f"Base URL: {base_url}")

    if args.test == "list-models":
        await test_list_models(cred, base_url)
    elif args.test == "max-output-tokens":
        values = [64000, 65536, 100000, 128000, 131072]
        if args.values:
            values = [int(v.strip()) for v in args.values.split(",")]
        await test_max_output_tokens(cred, base_url, args.model, values)
    else:
        await test_prompt(
            cred, base_url, args.model, args.prompt, args.max_output_tokens, args.stream
        )


if __name__ == "__main__":
    asyncio.run(main())
