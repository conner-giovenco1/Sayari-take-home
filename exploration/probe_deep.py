"""Run every read-only deep call on a few entities and print what comes back."""
import json
import sys
from collections import Counter

sys.path.insert(0, "src")
from probe_helpers import CACHE, raw


def top_id(res_name):
    d = json.loads((CACHE / f"res_{res_name}.json").read_text())
    best = max(d["data"], key=lambda x: x["score"])
    return best["entity_id"], best["label"]


targets = {k: top_id(k) for k in ["sber_pjsc", "rosneft", "huawei", "universal", "spotify", "bosch_gmbh", "yanfeng", "magna", "lear_corp", "hyundai"]}
pick = sys.argv[1:] or list(targets)

for key in pick:
    eid, label = targets[key]
    print(f"\n===== {key}: {label} ({eid})")

    d, t, e = raw(f"summary_{key}", "GET", f"/v1/entity_summary/{eid}")
    if e:
        print("  summary ERR", e)
    else:
        risk = d.get("risk", {})
        print(f"  summary {t:.1f}s | type={d.get('type')} countries={d.get('countries')} sanctioned={d.get('sanctioned')} pep={d.get('pep')}")
        print(f"  counts: relationships={d.get('related_entities_count')} sources={d.get('source_count')} trade={d.get('trade_count')} psa={d.get('psa_count')}")
        print(f"  relationship types: {dict(Counter(d.get('relationship_count', {})).most_common(6))}")
        lv = Counter(v.get("level") for v in risk.values())
        print(f"  {len(risk)} risk flags by level {dict(lv)}")
        for k, v in sorted(risk.items(), key=lambda kv: {"critical": 0, "high": 1, "elevated": 2, "relevant": 3}.get(kv[1].get("level"), 4))[:8]:
            print(f"    - {k} [{v.get('level')}] value={v.get('value')}")

    d, t, e = raw(f"ubo_{key}", "GET", f"/v1/ubo/{eid}", params={"limit": 20})
    print(f"  UBO {t:.1f}s:", e or f"{len(d['data'])} owner paths, explored={d.get('explored_count')}, partial={d.get('partial_results')}")
    if d and not e:
        for p in d["data"][:4]:
            tgt = p["target"]
            print(f"    owner: {tgt.get('label','')[:50]} ({tgt.get('type')}, {tgt.get('countries')}) hops={len(p['path'])} sanctioned={tgt.get('sanctioned')}")

    d, t, e = raw(f"downstream_{key}", "GET", f"/v1/downstream/{eid}", params={"limit": 20})
    print(f"  downstream {t:.1f}s:", e or f"{len(d['data'])} owned paths, explored={d.get('explored_count')}, partial={d.get('partial_results')}")

    d, t, e = raw(f"watchlist_{key}", "GET", f"/v1/watchlist/{eid}", params={"limit": 20})
    print(f"  watchlist {t:.1f}s:", e or f"{len(d['data'])} paths to watchlisted entities, explored={d.get('explored_count')}")
    if d and not e:
        for p in d["data"][:3]:
            tgt = p["target"]
            print(f"    hit: {tgt.get('label','')[:50]} ({tgt.get('countries')}) hops={len(p['path'])} via {[s.get('field') for s in p['path']]}")

    d, t, e = raw(f"upstream_{key}", "GET", f"/v1/supply_chain/upstream/{eid}", params={"max_depth": 1, "limit": 20})
    if e:
        print("  upstream ERR", e)
    else:
        paths = d.get("data", {}).get("paths", [])
        ents = d.get("data", {}).get("entities", {})
        print(f"  upstream {t:.1f}s: {len(paths)} supplier paths, {len(ents)} entities, explored={d.get('explored_count')}, partial={d.get('partial_results')}")
        hs = Counter(c["hs_code"][:4] for p in paths for step in p["path"] for c in step.get("components", []))
        origin = Counter(cc for p in paths for step in p["path"] for c in step.get("components", []) for cc in c.get("departure_countries", []))
        print(f"    HS codes {dict(hs.most_common(5))} | ship-from {dict(origin.most_common(6))}")
        risky = [(v.get("label", "")[:40], sorted(v.get("risk", {}))[:3]) for v in ents.values() if v.get("risk")]
        print(f"    suppliers with risk flags: {len(risky)} e.g. {risky[:2]}")

    for kind, filt in [("suppliers", {"buyer_id": [eid]}), ("buyers", {"supplier_id": [eid]})]:
        d, t, e = raw(f"trade_{kind}_{key}", "POST", f"/v1/trade/search/{kind}", params={"limit": 20}, body={"filter": filt})
        if e:
            print(f"  trade {kind} ERR", e)
        else:
            size = d.get("size", {})
            print(f"  trade {kind} {t:.1f}s: {size.get('count')} ({size.get('qualifier')}) | e.g. {[(r.get('label','')[:30], r.get('countries')) for r in d['data'][:3]]}")

    d, t, e = raw(f"news_{key}", "GET", f"/v1/negative_news/{eid}")
    print(f"  negative news {t:.1f}s:", e or str(d)[:300])
