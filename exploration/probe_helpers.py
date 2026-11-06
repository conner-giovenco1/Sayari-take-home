"""Shared helpers for exploring the Sayari API. Every response is saved to cache/probe/ as JSON."""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from sayari.client import Sayari

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "cache" / "probe"
CACHE.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")

client = Sayari(client_id=os.environ["SAYARI_CLIENT_ID"], client_secret=os.environ["SAYARI_CLIENT_SECRET"])


import httpx

BASE = "https://api.sayari.com"
_token = {"value": None, "expires": 0}


def token():
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    r = httpx.post(f"{BASE}/oauth/token", json={
        "client_id": os.environ["SAYARI_CLIENT_ID"], "client_secret": os.environ["SAYARI_CLIENT_SECRET"],
        "audience": "sayari.com", "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    body = r.json()
    _token.update(value=body["access_token"], expires=time.time() + body.get("expires_in", 3600))
    return _token["value"]


def raw(name, method, path, params=None, body=None, retries=4, timeout=90):
    """Call the API directly (no SDK), handle 429s, cache JSON. Returns (data, seconds, error)."""
    cache = CACHE / f"{name}.json"
    if cache.exists():
        return json.loads(cache.read_text()), 0.0, None
    start = time.time()
    for _ in range(retries):
        try:
            r = httpx.request(method, f"{BASE}{path}", params=params, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {token()}"})
        except httpx.TimeoutException:
            return None, time.time() - start, f"TIMEOUT after {timeout}s"
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 10)))
            continue
        break
    secs = time.time() - start
    if r.status_code >= 400:
        return None, secs, f"HTTP {r.status_code}: {r.text[:300]}"
    data = r.json()
    cache.write_text(json.dumps(data, indent=1))
    return data, secs, None


def to_plain(obj):
    """Turn SDK response objects into plain dicts/lists."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    if hasattr(obj, "dict"):
        return obj.dict(by_alias=True, exclude_none=True)
    if isinstance(obj, list):
        return [to_plain(o) for o in obj]
    return obj


def call(name, fn, *args, **kwargs):
    """Run one API call, save the JSON, return (data, seconds, error)."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text()), 0.0, None
    start = time.time()
    try:
        data = to_plain(fn(*args, **kwargs))
    except Exception as e:  # keep going; record the failure
        return None, time.time() - start, f"{type(e).__name__}: {str(e)[:300]}"
    path.write_text(json.dumps(data, indent=1, default=str))
    return data, time.time() - start, None
