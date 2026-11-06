"""Smoke test: can we log in to Sayari and look up one company?

Run from take_home_code/:  .venv/bin/python src/check_connection.py
"""
import os

from dotenv import load_dotenv
from sayari.client import Sayari

load_dotenv()
client_id = os.getenv("SAYARI_CLIENT_ID")
client_secret = os.getenv("SAYARI_CLIENT_SECRET")
if not client_id or not client_secret:
    raise SystemExit("Missing keys: paste them into take_home_code/.env first.")

client = Sayari(client_id=client_id, client_secret=client_secret)

# 1. Match a company to a Sayari record (results are not sorted, so take the best score).
result = client.resolution.resolution(name="Robert Bosch GmbH", country=["DEU"], type=["company"], limit=5)
best = max(result.data, key=lambda m: m.score)
print(f"Best match: {best.label} | id={best.entity_id} | score={best.score} | strength={best.match_strength}")

# 2. Pull its profile.
entity = client.entity.get_entity(best.entity_id)
print(f"Countries: {entity.countries} | sanctioned={entity.sanctioned} | pep={entity.pep}")
print(f"Risk flags: {sorted((entity.risk or {}).keys())}")

# 3. Check the graph endpoints work with these keys (they have a tighter rate limit).
ubo = client.traversal.ubo(best.entity_id, limit=5)
print(f"UBO call OK: {len(ubo.data)} owner paths returned")
print("Connection OK.")
