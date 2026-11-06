"""Step 3: turn the raw Sayari data into one row per supplier.

For each supplier we answer four plain questions:
  1. Is it risky itself?            -> "direct" flags (Sayari's seed flags: the company itself is on a list)
  2. Is risk close to it?           -> "network" flags: Sayari gives the number of hops to the nearest risky party
  3. Who, concretely?               -> watchlisted companies reachable through ownership or trade links (<= 2 hops)
  4. What about its own suppliers?  -> direct shipping suppliers that carry forced-labor flags

Trade links that decide a level are re-dated from Sayari's shipment search (refine_with_shipments), because the
relationship summary under-reports later shipments.

Generic `linked_to` links are ignored: in testing they connected Spotify to General Dynamics.

Run from take_home_code/:  .venv/bin/python src/score.py
Output: output/supplier_risk.csv, output/watchlist_links.csv
"""
import re
from functools import cache

import pandas as pd

from collect import collect_supplier, record_ids, shipments
from sayari_api import call


@cache
def risk_factors():
    """Sayari's catalogue of risk flags (id -> level, categories, type), fetched once and cached."""
    response = call("GET", "/v1/ontology/risk_factors")
    if response is None:  # missing keys are caught earlier, in sayari_api
        raise SystemExit("Could not fetch Sayari's risk-factor catalogue; see the error above.")
    table = pd.DataFrame(response["data"]).set_index("id")
    table["categories"] = table["categories"].apply("|".join)
    return table


# Relationship types, by direction. A chain must keep one direction (or go down to a subsidiary and then
# trade): "owned by the owner of X" is a sibling (a shared investor), not exposure. Everything else
# (linked_to, etc.) is ignored.
OWNED_BY = {"has_shareholder", "has_beneficial_owner", "has_owner", "subsidiary_of", "branch_of"}   # going up
OWNS = {"shareholder_of", "beneficial_owner_of", "owner_of", "has_subsidiary", "has_branch"}          # going down
BUYS_FROM = {"receives_from"}   # toward its suppliers
SELLS_TO = {"ships_to"}         # toward its customers
TRADE = BUYS_FROM | SELLS_TO
CONTROLS = {"has_subsidiary", "has_branch"}  # control, not just a shareholding
DIRECTIONS = [OWNED_BY, OWNS, BUYS_FROM, SELLS_TO]
SAME_AS = "possibly_same_as"  # a duplicate record of the same company: doesn't count as a hop
MAX_HOPS = 2

# The client is a Western carmaker, so count US, EU and UK sanctions and export-control lists.
# Other lists (e.g. Russia's or China's counter-sanctions on Western firms) are not the client's legal exposure.
RELEVANT_LISTS = ("sanctioned_usa_", "sanctioned_eu_", "sanctioned_gbr_", "usa_bis", "usa_isn", "usa_aeca_debarred",
                  "usa_section_1260h",
                  "cmic_entity")
COUNTED_LEVELS = {"high", "critical"}  # "elevated" flags are mostly product/country-level warnings
NOT_OUR_JURISDICTION = ("_jpn_", "fsb_rf")  # Japanese and Russian export-control lists don't bind a US/EU client

# Sayari's risk categories, grouped into the four themes the report talks about.
THEMES = {
    "sanctions": {"sanctions", "sanctions_and_export_control_lists"},
    "export_controls": {"export_controls"},
    "forced_labor": {"forced_labor"},
    "political_or_regulatory": {"political_exposure", "regulatory_action", "adverse_media"},
}


def theme_of(flag_id):
    """Which report theme a Sayari risk flag belongs to (or None)."""
    if flag_id not in risk_factors().index:
        return None
    categories = set(str(risk_factors().loc[flag_id, "categories"]).split("|"))
    return next((theme for theme, cats in THEMES.items() if cats & categories), None)


def hops(flag):
    """How many hops away a flag's risky party is: 0 = the company itself, n = n hops away.

    Booleans on network/PSA flags count as 1."""
    value = flag.get("value")
    if isinstance(value, bool):
        return 1
    return value if isinstance(value, int) else None


def flag_summary(profile):
    """Closest distance per theme, split into direct (on the company itself) and network (nearby)."""
    out = {}
    for flag_id, flag in (profile.get("risk") or {}).items():
        theme = theme_of(flag_id)
        if theme is None or flag.get("level") not in COUNTED_LEVELS:
            continue
        if theme == "export_controls" and any(tag in flag_id for tag in NOT_OUR_JURISDICTION):
            continue
        kind = risk_factors().loc[flag_id, "risk_type"]
        distance = 0 if kind == "seed" else hops(flag)
        if distance is None:
            continue
        key = f"{theme}_hops"
        out[key] = min(out.get(key, 99), distance)
    return out


INVASION = "2022-02-24"  # broad Russia sanctions start here; older trade is history, not current exposure


def is_listed(target):
    """The US/EU/UK lists a party is on itself.

    Seed flags only: network flags such as usa_bis_50_percent_rule mean 'owned by a listed party', not 'listed'."""
    catalogue = risk_factors()
    return sorted(k for k in (target.get("risk") or {})
                  if k.startswith(RELEVANT_LISTS) and k in catalogue.index and catalogue.loc[k, "risk_type"] == "seed")


def listed_since(target):
    """The earliest date Sayari gives for a party's US/EU/UK listings, or None if it gives none.

    Sayari stores the date on each list flag (risk metadata `from_date`). These matched official sources for every
    party checked by hand, except KAMAZ, where Sayari's record misses the EU's 16 Mar 2022 trade ban."""
    risk = target.get("risk") or {}
    dates = []
    for flag in is_listed(target):
        value = (risk[flag].get("metadata") or {}).get("from_date")
        dates += [d[:10] for d in (value if isinstance(value, list) else [value]) if d]
    return min(dates, default=None)


def own_relationship(step):
    """The relationship data for the step's own type only.

    A step can also carry other types between the same two companies (a mirrored receives_from, a shareholding),
    which must not leak into its dates or counts."""
    return step["relationships"].get(step["field"]) or {}


def day(date):
    """A Sayari date in comparable form: a month-only 'YYYY-MM' counts as that month's last day."""
    return date if len(date) >= 10 else date + "-31" if len(date) == 7 else date + "-12-31"


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
COVERAGE = re.compile(r"\((?:(?P<m1>[A-Za-z]+) )?(?P<y1>\d{4})\s*(?:-\s*(?:(?P<m2>[A-Za-z]+) )?(?P<y2>\d{4}|present))?\)",
                      re.IGNORECASE)
SOURCE_WINDOWS = {}  # Sayari source id -> (first day it covers, last day or None), from labels like
                     # "Kazakhstan Imports & Exports (January 2023 - Present)"; filled by learn_sources()


def learn_sources(data):
    """Record the date window of every Sayari source whose label appears anywhere in `data`.

    Profiles and watchlist entities carry a source_count with labels. A record's source bounds its shipment date."""
    if isinstance(data, dict):
        for source_id, info in (data.get("source_count") or {}).items():
            match = COVERAGE.search((info or {}).get("label", "")) if isinstance(info, dict) else None
            if match:
                month1, month2 = MONTHS.get((match["m1"] or "").lower(), 1), MONTHS.get((match["m2"] or "").lower(), 12)
                end = None if (match["y2"] or "").lower() == "present" else f"{match['y2'] or match['y1']}-{month2:02d}-31"
                SOURCE_WINDOWS[source_id] = (f"{match['y1']}-{month1:02d}-01", end)
        for value in data.values():
            learn_sources(value)
    elif isinstance(data, list):
        for value in data:
            learn_sources(value)


def latest_bounds(values):
    """The (earliest, latest) date the most recent shipment can be, when no shipment date says so directly.

    Worked out from each record's source window and acquisition date. Sayari returns at most 1,000 records per link,
    so with 1,000 the unseen ones leave the upper bound unknown."""
    lower, uppers = None, []
    for v in values:
        start, end = SOURCE_WINDOWS.get((v.get("record") or "").split("/")[0], (None, None))
        if start and (lower is None or start > lower):
            lower = start
        caps = [d for d in (end, v.get("acquisition_date")) if d]
        uppers.append(min(caps, key=day) if caps else None)
    upper = None if not uppers or None in uppers or len(values) >= 1000 else max(uppers, key=day)
    return lower, upper


def step_dates(step):
    """A trade link's first and latest shipment dates, from Sayari's relationship summary.

    Returns (first shipment, latest shipment, basis); basis says whether the latest date is an observed "shipment",
    or a bound: "at least" / "at most". Each record's from_date is the relationship's FIRST shipment, copied onto
    every record; trade_metadata.dates adds some later dates but is only a partial list (the shipment search is the
    reliable source; see refine_with_shipments). With several records and no trade_metadata, the latest shipment is
    bounded by the records' sources ("... (January 2023 - Present)") and acquisition dates: if it must be after the
    invasion, the lower bound is used; if it must be before, the upper bound; otherwise it is unknown (None). A
    record from a source that starts later than every listed date (e.g. a 2023+ dataset) also lifts the latest date
    to that source's start."""
    rel = own_relationship(step)
    meta = [d[:10] for d in ((rel.get("trade_metadata") or {}).get("dates") or [])]
    values = rel.get("values", [])
    records = [(v.get("date") or v.get("from_date"))[:10] for v in values if v.get("date") or v.get("from_date")]
    if not meta and not records:
        return None, None, None
    first = min(meta + records, key=day)
    source_start, upper = latest_bounds(values)
    if meta or len(values) == 1 or len(set(records)) > 1:
        latest = max(meta + records, key=day)
        if source_start and day(source_start) > day(latest):
            return first, source_start, "at least"
        return first, latest, "shipment"
    lower = max([first] + ([source_start] if source_start else []), key=day)
    if day(lower) >= INVASION:
        return first, lower, "at least"
    if upper and day(upper) < INVASION:
        return first, upper, "at most"
    return first, None, None


ONGOING = "ongoing"  # an ownership chain Sayari doesn't mark as ended


def trade_evidence(steps):
    """How much trade data backs the chain's trade link(s).

    Returns Sayari's record count, and the first shipment date when the chain has one trade link (a direct trade, or
    a subsidiary's trade; the latest shipment is chain_date)."""
    trade_steps = [s for s in steps if s["field"] in TRADE]
    # Sayari's count can be lower than the records it returns, and the records stop at 1,000: take the larger.
    records = sum(max((own_relationship(s).get("trade_metadata") or {}).get("count") or 0,
                      len(own_relationship(s).get("values", []))) for s in trade_steps)
    first = step_dates(trade_steps[0])[0] if len(trade_steps) == 1 else None
    return records, first


def combine_latest(latest):
    """A chain's date from its trade links' (latest date, basis) pairs: as recent as its least recent link.

    A link known to end before the invasion dates the chain even if another link is unknown; otherwise any unknown
    link makes the chain unknown -> (None, None)."""
    known = [(d, b) for d, b in latest if d]
    oldest = min(known, key=lambda x: day(x[0])) if known else (None, None)
    if known and day(oldest[0]) < INVASION:
        return oldest
    return (None, None) if len(known) < len(latest) else oldest


def chain_date(steps):
    """When the chain was last active.

    Only trade links carry a meaningful date (the latest shipment); an ownership link's date is when the stake
    began, and a stake that hasn't ended is current. So:
    - a chain with trade links is only as recent as its least recent trade link:
      Bosch -> SABIC (last shipment 2023) -> buyer (last shipment 2019) dates to 2019
    - a pure ownership chain (ended stakes are already excluded) is ONGOING.
    Returns (date, basis): basis is "shipment", "at least", "at most" (see step_dates), "current stake" or None."""
    latest = [step_dates(s)[1:] for s in steps if s["field"] in TRADE]
    return combine_latest(latest) if latest else (ONGOING, "current stake")


def is_former(step):
    """True if Sayari marks every record of this link as ended (e.g. a divested shareholding)."""
    values = own_relationship(step).get("values", [])
    return bool(values) and all(v.get("former") for v in values)


def link_type(steps):
    """What kind of chain this is, or None if it should be ignored.

    - 'ownership': every step up the ownership chain, or every step down it
    - 'trade': every step toward suppliers, or every step toward customers
    - 'subsidiary trade': down to a subsidiary or branch it controls, then one trade link ("its subsidiary
      ships to X"). A minority shareholding followed by trade is not counted: the supplier doesn't control it.
    Anything else is a sibling (two companies that share an owner, a supplier, or a customer)."""
    if any(all(s in direction for s in steps) for direction in DIRECTIONS):
        return "ownership" if steps[0] in OWNED_BY | OWNS else "trade"
    if len(steps) >= 2 and all(s in CONTROLS for s in steps[:-1]) and steps[-1] in TRADE:
        return "subsidiary trade"
    return None


def close_watchlist_links(watchlist):
    """The best chain to each US/EU/UK-listed party within MAX_HOPS, one row per party.

    Best = the closest chain, and among equally close ones the most recent."""
    best = {}
    for link in valid_links(watchlist):
        key = link["sayari_url"]
        # Among equally close paths: a current stake, then the most recent post-invasion trade, then an undated
        # one (never assumed old), then the most recent older trade.
        date = link["chain_date"]
        recency = 3 if date == ONGOING else 1 if date is None else 2 if day(date) >= INVASION else 0
        rank = (-link["hops"], recency, day(date) if date not in (None, ONGOING) else "")
        if key not in best or rank > best[key][0]:
            best[key] = (rank, link)
    return [link for _, link in best.values()]


def valid_links(watchlist):
    """Every path to a US/EU/UK-listed party that is a valid chain (see link_type) of at most MAX_HOPS."""
    links = []
    for path in (watchlist or {}).get("data", []):
        real = [step for step in path["path"] if step["field"] != SAME_AS]
        fields = [step["field"] for step in real]
        kind = link_type(fields) if fields else None
        if kind is None or len(fields) > MAX_HOPS or any(is_former(s) for s in real):
            continue
        target = path["target"]
        lists = is_listed(target)
        if not lists:
            continue
        date, basis = chain_date(real)
        records, first_trade = trade_evidence(real)
        # Who trades with whom on each trade step (the step's entity is the company it reaches), for the shipment search.
        pairs, previous = [], path.get("from_record")
        for step in path["path"]:
            if step["field"] != SAME_AS and step["field"] in TRADE:
                pairs.append((step["field"], previous, step["entity"]["id"]))
            previous = step["entity"]["id"]
        links.append({
            "counterparty": target.get("label"),
            "counterparty_en": target.get("translated_label") or target.get("label"),
            "hops": len(fields),
            "via": " > ".join(fields),
            "link_type": kind,
            "through": " > ".join(s["entity"].get("label", "")[:40] for s in real[:-1]),
            "chain_date": date,
            "date_basis": basis,  # "shipment" = observed; "at least"/"at most" = bound from sources/acquisition
            "since_invasion": bool(date and (date == ONGOING or day(date) >= INVASION)),
            "trade_records": records,
            "first_trade": first_trade,
            "lists": ", ".join(lists),
            "listed_since": listed_since(target),  # Sayari's earliest US/EU/UK listing date
            "sayari_url": target.get("entity_url"),
            "from_record": path.get("from_record"),
            "_steps": real, "_pairs": pairs,
        })
    return links


def first_value(value):
    """A field's first value: Sayari gives some fields as a list and others as a single value."""
    return (value[0] if value else "") if isinstance(value, list) else (value or "")


def shipment_day(shipment):
    """A shipment's date: its departure (dispatch) date when it has a full one, else its customs arrival date.

    Customs data usually carries only one; Russian import records, for example, carry the arrival date."""
    dates = [first_value(shipment.get(k))[:10] for k in ("departure_date", "arrival_date")]
    full = [d for d in dates if len(d) == 10]  # a month-only "2023-07" is less precise than a full date
    return (full or [d for d in dates if d] or [None])[0]


def refine_with_shipments(link, own_records=()):
    """Re-date a link from its actual shipments (Sayari's shipment search).

    The relationship summary under-reports later dates. Done for every direct trade, every subsidiary trade, and
    every two-hop chain not already dated after the invasion: the links whose date decides a level or a claim in the
    report. Falls back to the summary dates when the search finds no shipments for a step. A direct trade is
    searched from every Sayari record of the supplier (own_records), since one company's shipments can be split
    across its records, and in both directions, since the path Sayari returns for a party shows only one of them (ZF
    buys from ZF Kama and also ships to it)."""
    # Skip pure ownership links (no shipments to search) and two-hop trade chains already dated after the invasion:
    # a later date can't change their level.
    if not link["_pairs"] or (link["hops"] == 2 and link["link_type"] == "trade" and link["since_invasion"]):
        return link
    latest, total, first = [], 0, []
    for (field, frm, to), step in zip(link["_pairs"], [s for s in link["_steps"] if s["field"] in TRADE]):
        # A direct trade is searched from every record of the supplier, since its shipments can be split across them.
        starts = list(dict.fromkeys([frm] + list(own_records))) if link["hops"] == 1 else [frm]
        rows, complete, any_found = {}, True, False
        pairs = [(start, to) if field in SELLS_TO else (to, start) for start in starts]
        if link["hops"] == 1:
            # Both directions: Sayari's path shows only one, but trade can run both ways (ZF buys from ZF Kama
            # and also ships to it).
            pairs += [(buyer, seller) for seller, buyer in pairs]
        for seller, buyer in pairs:
            got = shipments(seller, buyer)
            if got:
                any_found = True
                rows.update({s["id"]: s for s in got[0]})
                complete = complete and got[1]
        found = (list(rows.values()), complete) if any_found else None
        dates = sorted(d for d in (shipment_day(s) for s in (found or ([], True))[0]) if d)
        if dates:
            latest.append((dates[-1], "shipment" if found[1] else "at least"))
            first.append(dates[0])
            total += len(found[0])
        else:  # the search found no shipments for this step: fall back to the relationship summary's dates
            first_date, last_date, basis = step_dates(step)
            latest.append((last_date, basis))
            first += [first_date] if first_date else []
    # The chain is only as recent as its least recent trade step.
    date, basis = combine_latest(latest)
    link.update({"chain_date": date, "date_basis": basis, "since_invasion": bool(date and day(date) >= INVASION)})
    if len(link["_pairs"]) == 1 and total:
        link.update({"trade_records": total, "first_trade": min(first, key=day)})
    return link


def after_listing(link):
    """Whether the link was still active after the party was listed: "yes", "no", "current stake" or "unknown".

    Compares the link's latest date with Sayari's listing date. A bound that can't settle it ("at least" a date
    before the listing, "at most" a date after it) is "unknown"."""
    date, basis, listed = link["chain_date"], link["date_basis"], link["listed_since"]
    if date == ONGOING:
        return "current stake"
    if not date or not listed:
        return "unknown"
    after = day(date) > listed
    if (after and basis == "at most") or (not after and basis == "at least"):
        return "unknown"
    return "yes" if after else "no"


def supplier_summary(upstream):
    """Count a supplier's direct shippers, and how many carry labor-risk flags.

    Labor risk = Sayari's forced_labor category, which also covers child labor. Counted separately by level: the
    high/critical ones here are network flags (the shipper owns, or is owned by, a company tied to forced labor);
    the elevated ones are almost all US Labor Dept 'goods produced with forced/child labor' product-country flags."""
    data = (upstream or {}).get("data") or {}
    paths, entities = data.get("paths", []), data.get("entities", {})
    supplier_ids = {step["entity_id"] for p in paths for step in p["path"]}
    catalogue = risk_factors()

    def labor_levels(entity_id):
        """The levels of one shipper's forced-labor flags, e.g. {"high", "elevated"}."""
        flags = entities.get(entity_id, {}).get("risk_factors", [])
        return {catalogue.loc[f, "level"] for f in flags if theme_of(f) == "forced_labor"}

    levels = {i: labor_levels(i) for i in supplier_ids}
    origins = sorted({c for p in paths for step in p["path"] for comp in step.get("components", [])
                      for c in comp.get("departure_countries", [])})
    return {"direct_suppliers": len(supplier_ids),
            "suppliers_labor_flag_high": sum(bool(lv & COUNTED_LEVELS) for lv in levels.values()),
            "suppliers_labor_flag_elevated_only": sum(bool(lv) and not lv & COUNTED_LEVELS for lv in levels.values()),
            "supplier_ship_from": " ".join(origins)}


def link_counts(links):
    """Count listed counterparties by closeness (direct = 1 hop) and by date.

    Dates: since / before the invasion, or undated when Sayari has no date on a trade link (never assumed old)."""
    direct = [l for l in links if l["hops"] == 1]
    return {
        "direct_links_since_invasion": sum(l["since_invasion"] for l in direct),
        "direct_links_before_invasion": sum(l["chain_date"] is not None and not l["since_invasion"] for l in direct),
        "direct_links_undated": sum(l["chain_date"] is None for l in direct),
        "two_hop_links_since_invasion": sum(l["hops"] == 2 and l["since_invasion"] for l in links),
        "two_hop_links_undated": sum(l["hops"] == 2 and l["chain_date"] is None for l in links),
    }


def within(row, column, max_hops):
    """True if the supplier has a high/critical flag of this theme within max_hops (0 = on itself; None = no flag)."""
    hops = row.get(column)
    return hops is not None and hops <= max_hops


def exposure_level(row):
    """Label a supplier "high", "medium" or "low" with simple rules a reviewer can audit.

    Rules, in order:
    high   = sanctioned itself, or a DIRECT ownership/trade link to a US/EU/UK-listed party dated after 24 Feb 2022
    medium = an older or undated direct link, a 2-hop link since 24 Feb 2022 or of unknown date (undated is never
             assumed old), a high/critical export-control flag within 2 hops, or a high/critical forced-labor flag
             within 3 hops
    low    = everything else
    """
    if row.get("sanctioned") or row["direct_links_since_invasion"] > 0:
        return "high"
    if (row["direct_links_before_invasion"] > 0 or row["direct_links_undated"] > 0
            or row["two_hop_links_since_invasion"] > 0 or row["two_hop_links_undated"] > 0
            or within(row, "export_controls_hops", 2) or within(row, "forced_labor_hops", 3)):
        return "medium"
    return "low"


def main():
    """Score every supplier; write output/supplier_risk.csv and output/watchlist_links.csv."""
    matches = pd.read_csv("output/matches.csv")
    # Served from cache; pooled over each supplier's records. Learn every source's date window first, so each
    # supplier's links are dated with the same knowledge whatever the list order.
    collected = {m.input_name: collect_supplier(m) for m in matches.itertuples() if isinstance(m.entity_id, str)}
    learn_sources(list(collected.values()))
    rows, all_links = [], []
    for m in matches.itertuples():
        if not isinstance(m.entity_id, str):  # no Sayari match: keep the supplier visible, never "low"
            rows.append({"supplier": m.input_name, "match_confidence": m.confidence, "exposure": "incomplete - no match"})
            continue
        data = collected[m.input_name]
        profiles = data["profiles"] or []
        flags = [flag_summary(p) for p in profiles]
        links = [refine_with_shipments(link, record_ids(m)) for link in close_watchlist_links(data["watchlist"])]
        for link in links:
            link["after_listing"] = after_listing(link)
        row = {"supplier": m.input_name, "sayari_record": m.entity_label, "match_confidence": m.confidence,
               "records_used": len(record_ids(m)),
               # Listed itself on a US/EU/UK list (Sayari's own "sanctioned" flag also covers other countries' lists).
               "sanctioned": any(is_listed(p) for p in profiles),
               "risk_flags": len({k for p in profiles for k in (p.get("risk") or {})}),
               # Closest high/critical flag per theme, in hops (empty = none); a fixed set of columns.
               **{f"{t}_hops": min((f[f"{t}_hops"] for f in flags if f"{t}_hops" in f), default=None) for t in THEMES},
               **link_counts(links),
               **supplier_summary(data["suppliers"])}
        # Missing data must never look like "no risk": a failed call marks the supplier incomplete.
        complete = all(v is not None for v in data.values()) and data["watchlist"].get("complete")
        row["exposure"] = exposure_level(row) if complete else "incomplete - rerun collect.py"
        # "Low" can mean "Sayari knows little about this record", not "clean".
        row["thin_data"] = m.total_data_volume < 5000 and row["direct_suppliers"] == 0
        # A shipper list that filled the API's limit may be cut off; the labor counts are then a floor.
        row["suppliers_cut_off"] = data["suppliers"] is not None and not data["suppliers"]["complete"]
        rows.append(row)
        all_links += [{"supplier": m.input_name, **{k: v for k, v in l.items() if not k.startswith("_")}}
                      for l in links]

    columns = list(max(rows, key=len))  # a full row's column order, even if the first supplier has no match
    table = pd.DataFrame(rows)[columns]
    counts = [c for c in columns if c.startswith(("direct_", "two_hop_", "suppliers_labor_"))]
    table[counts] = table[counts].astype("Int64")  # whole numbers, even when a no-match row leaves them empty
    table.to_csv("output/supplier_risk.csv", index=False)
    pd.DataFrame(all_links).to_csv("output/watchlist_links.csv", index=False)
    print(table["exposure"].value_counts().to_string())
    incomplete = table[table.exposure.str.startswith("incomplete")].supplier.tolist()
    if incomplete:
        print(f"WARNING: data missing for {incomplete}; their rows are marked incomplete, not scored.")


if __name__ == "__main__":
    main()
