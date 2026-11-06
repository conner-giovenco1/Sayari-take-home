"""Step 2: pull the risk data for every matched supplier.

Three kinds of data per Sayari record (every call cached to cache/api/, so reruns are free); the watchlist takes one
query per pattern (six) plus extra pages, so a first run makes roughly 8+ requests per record. When one company
is split across several Sayari records (see resolve.py), all of them are read and pooled:
  - profile:   /v1/entity_summary/{id}    -> its own risk flags, incl. network flags with the hop count
  - watchlist: /v1/watchlist/{id}         -> paths (<= 2 hops, current relationships only) to parties on a sanctions
                                             or watch list, asked once per valid chain pattern (below)
  - suppliers: /v1/supply_chain/upstream  -> every shipper to it (depth 1, one large call), with risk flags

The watchlist filters run on Sayari's side. Unfiltered, the endpoint returns 50 paths per page, mostly 3-4 hops
long through generic links, so close links could be cut off by the page limit. It also returns only ONE path per
listed party, so a query mixing all relationship types can hand back a sibling chain ("its supplier also supplies
X") while a valid one exists. Asking once per pattern, with each pattern using as few relationship types as
possible, keeps most returned paths valid. The API can't constrain the ORDER of types within a path, though, so in
the two subsidiary-trade queries a plain trade path (or a sibling) often takes a party's slot: the subsidiary-trade
links found are a minimum, not a complete list.

Run from take_home_code/:  .venv/bin/python src/collect.py
"""
import time

import pandas as pd

from sayari_api import call

# One watchlist query per valid chain pattern. Generic links (linked_to, officers, etc.) are never followed.
PATTERNS = {
    "owned_by": ["has_shareholder", "has_beneficial_owner", "has_owner", "subsidiary_of", "branch_of"],
    "owns": ["shareholder_of", "beneficial_owner_of", "owner_of", "has_subsidiary", "has_branch"],
    "sells_to": ["ships_to"],
    "buys_from": ["receives_from"],
    # Split by trade direction: a single mixed query returned ~1,000 sibling paths (receives_from > ships_to) that
    # took listed parties' only slot and could hide a valid "its subsidiary ships to X".
    "subsidiary_sells": ["has_subsidiary", "has_branch", "ships_to"],
    "subsidiary_buys": ["has_subsidiary", "has_branch", "receives_from"],
}
PAGE = 50          # API maximum per page
MAX_OFFSET = 1000  # API maximum offset


def watchlist_pattern(entity_id, relationships):
    """Every watchlist path (at most 2 hops) for one relationship pattern, across all pages.

    Returns (paths, complete) or None if a call failed; a page Sayari marks partial makes it incomplete."""
    paths, offset, partial = [], 0, False
    while offset <= MAX_OFFSET:
        page = call("GET", f"/v1/watchlist/{entity_id}", params={
            "limit": PAGE, "offset": offset, "max_depth": 2, "relationships": relationships,
            "exclude_former_relationships": "true"})
        if page is None:
            return None
        paths += page.get("data", [])
        partial = partial or bool(page.get("partial_results"))
        if not page.get("next"):
            return paths, not partial
        offset += PAGE
    return paths, False  # more than ~1,000 paths: flagged in the output


def watchlist_paths(entity_id):
    """Watchlist paths from every pattern, pooled into one list.

    score.py keeps the best valid path per listed party."""
    pooled, complete = [], True
    for relationships in PATTERNS.values():
        result = watchlist_pattern(entity_id, relationships)
        if result is None:
            return None
        paths, done = result
        pooled += paths
        complete = complete and done
    return {"data": pooled, "complete": complete}


UPSTREAM_LIMIT = 10000  # paths per call; this endpoint ignores `offset`, so one large call has to hold them all


def upstream(entity_id):
    """Every direct shipper to one record (depth 1), in a single call.

    Each path is one shipper and one product, so a large supplier has several paths per shipper: Bosch has ~1,200
    paths from ~370 shippers, Schaeffler ~4,100. A response that fills the limit, or that Sayari marks partial, is
    marked incomplete. Returns None if the call failed."""
    response = call("GET", f"/v1/supply_chain/upstream/{entity_id}", params={"max_depth": 1, "limit": UPSTREAM_LIMIT})
    if response is None:
        return None
    data = response.get("data") or {}
    paths = data.get("paths", [])
    complete = len(paths) < UPSTREAM_LIMIT and not response.get("partial_results")
    return {"data": {"paths": paths, "entities": data.get("entities") or {}}, "complete": complete}


SHIPMENT_PAGE, SHIPMENT_MAX = 100, 5000


def shipments(seller_id, buyer_id):
    """Every shipment Sayari holds from one company to another, from the shipment search, page by page.

    Uses /v1/trade/search/shipments. Each shipment comes with its own arrival/departure date, HS code, description
    and weight. This is the reliable source of trade dates (a relationship's trade_metadata.dates is only a partial
    list). Returns (shipments, complete) or None if the first call failed; called from score.py for the links that
    matter, and cached like every other call."""
    rows, offset = {}, 0
    while offset < SHIPMENT_MAX:
        page = call("POST", "/v1/trade/search/shipments", params={"limit": SHIPMENT_PAGE, "offset": offset},
                    body={"filter": {"supplier_id": [seller_id], "buyer_id": [buyer_id]}})
        if page is None:
            return (list(rows.values()), False) if rows else None
        for shipment in page.get("data", []):
            rows[shipment["id"]] = shipment
        if len(page.get("data", [])) < SHIPMENT_PAGE:
            return list(rows.values()), True
        offset += SHIPMENT_PAGE
    return list(rows.values()), False


def collect(entity_id):
    """The three raw datasets for one Sayari record: its profile, its watchlist paths and its direct shippers."""
    return {
        "profile": call("GET", f"/v1/entity_summary/{entity_id}"),
        "watchlist": watchlist_paths(entity_id),
        "suppliers": upstream(entity_id),
    }


def record_ids(match):
    """The supplier's main record plus any other records of the same company (see resolve.py)."""
    others = match.other_records if isinstance(match.other_records, str) and match.other_records else ""
    return [match.entity_id] + [e for e in others.split("|") if e]


def collect_supplier(match):
    """All the data for one supplier: collect() for each of its Sayari records, pooled into one result.

    Any failed call makes that part None, so missing data is never mistaken for "no risk"."""
    parts = {e: collect(e) for e in record_ids(match)}
    profiles = [p["profile"] for p in parts.values()]
    watchlists = [p["watchlist"] for p in parts.values()]
    uppers = [p["suppliers"] for p in parts.values()]
    watchlist = None if None in watchlists else {
        # Each path keeps the record it starts from, so every link in the output can be traced in Sayari.
        "data": [{**path, "from_record": e} for e, w in zip(parts, watchlists) for path in w["data"]],
        "complete": all(w["complete"] for w in watchlists)}
    suppliers = None if None in uppers else {"data": {
        "paths": [path for u in uppers for path in (u.get("data") or {}).get("paths", [])],
        "entities": {k: v for u in uppers for k, v in ((u.get("data") or {}).get("entities") or {}).items()}},
        "complete": all(u["complete"] for u in uppers)}
    return {"profiles": None if None in profiles else profiles, "watchlist": watchlist, "suppliers": suppliers}


def main():
    """Collect (and cache) the data for every matched supplier, printing progress."""
    matches = pd.read_csv("output/matches.csv").dropna(subset=["entity_id"])
    start = time.time()
    for i, row in enumerate(matches.itertuples(), 1):
        data = collect_supplier(row)
        missing = [k for k, v in data.items() if v is None]
        paths = len((data["watchlist"] or {}).get("data", []))
        print(f"[{i}/{len(matches)}] {row.input_name:30s} {paths:4d} watchlist paths "
              f"{'missing: ' + ', '.join(missing) if missing else 'ok'}")
    print(f"done in {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
