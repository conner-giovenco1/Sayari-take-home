"""Small Sayari API client: login, retries on rate limits, and a disk cache.

The official SDK (sayari 0.1.43) crashes on some responses, so this calls the REST API directly.
Every response is saved under cache/api/, so rerunning the pipeline doesn't call the API again.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.sayari.com"
ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "cache" / "api"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")

_token = {"value": None, "expires_at": 0.0}


def _get_token():
    """Log in with the client ID/secret; reuse the token until it nearly expires."""
    if _token["value"] and time.time() < _token["expires_at"] - 60:
        return _token["value"]
    client_id, client_secret = os.getenv("SAYARI_CLIENT_ID"), os.getenv("SAYARI_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Set SAYARI_CLIENT_ID and SAYARI_CLIENT_SECRET in .env (copy .env.example).")
    response = httpx.post(f"{BASE_URL}/oauth/token", timeout=30, json={
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": "sayari.com",
        "grant_type": "client_credentials",
    })
    response.raise_for_status()
    body = response.json()
    _token["value"] = body["access_token"]
    _token["expires_at"] = time.time() + body.get("expires_in", 3600)
    return _token["value"]


def _cache_path(method, path, params, body):
    """The cache file for one request: the same method, path and parameters always map to the same file."""
    key = json.dumps([method, path, params, body], sort_keys=True)
    name = path.strip("/").replace("/", "_")[:60]
    return CACHE_DIR / f"{name}_{hashlib.md5(key.encode()).hexdigest()[:10]}.json"


def call(method, path, params=None, body=None, timeout=90, attempts=4):
    """Make one API call, served from the disk cache when it was made before.

    Returns the JSON body, or None if it still failed after retries (the error is printed). Rate limits (429) wait
    as long as the server asks; server errors (5xx) and timeouts back off 5s, 10s, 15s. Client errors (other 4xx)
    are not retried."""
    cache_file = _cache_path(method, path, params, body)
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    problem = "no attempt made"
    for attempt in range(1, attempts + 1):
        wait = None  # seconds to wait before retrying; None = no retry needed
        try:
            response = httpx.request(method, f"{BASE_URL}{path}", params=params, json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {_get_token()}"})
        except httpx.TimeoutException:
            problem, wait = f"timeout after {timeout}s", 5 * attempt
        except httpx.TransportError as error:  # dropped connection, DNS hiccup, etc.
            problem, wait = f"network error: {error}", 5 * attempt
        else:
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "10")  # seconds, or an HTTP date (then wait 10s)
                problem, wait = "rate limited", float(retry_after) if retry_after.isdigit() else 10.0
            elif response.status_code >= 500:
                problem, wait = f"HTTP {response.status_code}: {response.text[:200]}", 5 * attempt
        if wait is not None:
            if attempt < attempts:  # don't sleep after the last try
                time.sleep(wait)
            continue
        if response.status_code >= 400:
            print(f"  ! HTTP {response.status_code} on {path}: {response.text[:200]}")
            return None
        data = response.json()
        if isinstance(data, dict) and data.get("partial_results"):
            return data  # not cached: a rerun should ask again rather than reuse a cut-off answer
        partial = cache_file.with_suffix(".part")  # write then rename, so an interrupted run never leaves half a file
        partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        partial.replace(cache_file)
        return data

    print(f"  ! gave up on {path} after {attempts} attempts ({problem})")
    return None
