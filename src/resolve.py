"""Step 1: match each supplier in the list to the right Sayari record.

Why this isn't just "take the top search result":
- Sayari's top "strong" match was often the wrong company (Bosch -> Bosch Vietnam,
  Yanfeng -> a woven-bag packaging maker).
- Sister companies share an HQ address (Continental AG vs. Continental Finance GmbH).
- One real company can have several duplicate records; Sayari groups them with a shared `psa_id`
  ("possibly same as"), and only one of them holds most of the data.

- Duplicates are not always grouped: "CUMMINS INC" (402 data points) and "CUMMINS INC." (1.8M) share no psa_id.

So for each supplier we:
  1. search by each known legal name (current and former, e.g. Faurecia/Forvia) and the short name,
     with and without the address,
  2. if any candidate's name exactly matches a legal name, keep only those and pick the one Sayari knows most
     about, preferring records whose address matches; otherwise rank on a strict two-way name match + address,
  3. switch to the fullest record in its psa_id group,
  4. keep every other record with the exact legal name too: Sayari sometimes splits one company across
     records it doesn't group (two "BASF SE" records hold different shipments), so step 2 reads them all,
  5. label confidence from the record actually used, so a person reviews the weak matches.

Matches below "high" were then checked by hand (data/list_3_hand_checked.tsv: the records to use, the verdict and
why). Those records replace the search's pick; any other exact-legal-name records found are still pooled with them.

Run from take_home_code/:  .venv/bin/python src/resolve.py
Output: output/matches.csv
"""
import math
import re
import unicodedata
from pathlib import Path

import pandas as pd

from sayari_api import call

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
OUTPUT.mkdir(exist_ok=True)

# Legal forms (compared separately, see is_exact) and filler words: neither helps tell companies apart by name.
LEGAL_FORMS = {"inc", "corp", "co", "ltd", "gmbh", "ag", "se", "sa", "plc", "llc", "kg", "kgaa", "spa", "nv",
               "bv", "ab", "kk", "sab", "cv", "sl", "pty", "pvt"}
FORM_SYNONYMS = {"incorporated": "inc", "corporation": "corp", "company": "co", "limited": "ltd",
                 "private": "pvt"}
# "group"/"holdings" are ignored on purpose: a listed holding (American Axle & Manufacturing Holdings) and its main
# operating company (American Axle & Manufacturing, Inc.) are both "the supplier" for this screen.
STOPWORDS = LEGAL_FORMS | set(FORM_SYNONYMS) | {"group", "gruppe", "holdings", "the", "of", "and", "de"}


def words(text):
    """Lowercase words and numbers, accents and dots removed: "Automoción S.A." -> ["automocion", "sa"]."""
    plain = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    # A dot before a word is a separator ("CO.LTD" -> "co ltd"); other dots are abbreviations ("S.A." -> "sa").
    plain = re.sub(r"\.(?=[a-z]{2})", " ", plain).replace(".", "")
    return re.findall(r"[0-9a-z]+", plain)


def tokens(text):
    """The words that identify a company: everything except legal forms and filler."""
    return {w for w in words(text) if w not in STOPWORDS}


def overlap(a, b):
    """Share of the words in `a` that also appear in `b` (0 to 1)."""
    a, b = tokens(a), tokens(b)
    return len(a & b) / len(a) if a else 0.0


def name_match(wanted, found):
    """Two-way: 'Continental' vs 'Continental Finance' scores 0.5, not 1.0."""
    return min(overlap(wanted, found), overlap(found, wanted))


def legal_forms(text):
    """The legal-form words in a name ('Tenneco GmbH' -> {'gmbh'}), with synonyms folded together."""
    return {FORM_SYNONYMS.get(w, w) for w in words(text)} & LEGAL_FORMS


def is_exact(legal_name, label, address_match):
    """Same name AND same legal form. 'Tenneco GmbH' is not 'Tenneco Inc.' even though the words match.
    If either name has no legal form to compare, the address must match instead (at least half of the list
    address's words), so a namesake elsewhere ('INFINEON TECHNOLOGIES' in Sao Paulo) isn't taken for the company."""
    if name_match(legal_name, label) < 0.99:
        return False
    found, wanted = legal_forms(label), legal_forms(legal_name)
    if found and wanted:
        return found == wanted
    return address_match >= 0.5


def search(names, address, country):
    """All candidate records for one supplier, from several searches."""
    candidates = {}
    for name in names:
        for params in ({"name": name, "country": country},
                       {"name": name, "address": address, "country": country},
                       {"name": name, "address": address}):  # some records have no country set
            result = call("GET", "/v1/resolution", params={**params, "type": "company", "limit": 10})
            for match in (result or {}).get("data", []):
                candidates.setdefault(match["entity_id"], match)
    return list(candidates.values())


def profile(entity_id):
    return call("GET", f"/v1/entity_summary/{entity_id}") or {}


def data_volume(summary):
    """How much Sayari knows about a record: related entities + source documents."""
    sources = sum(s.get("count", 0) for s in (summary.get("source_count") or {}).values())
    return (summary.get("related_entities_count") or 0) + sources


def evidence_for(entity_id, legal_names, address):
    """How well one record matches the supplier: name, address, data volume."""
    summary = profile(entity_id)
    label = summary.get("label") or ""
    # Sayari's "countries" field lists every country a company trades with, so it can't tell
    # Bosch Germany from Bosch India. The address is the reliable signal.
    address_match = round(overlap(address, " ".join(summary.get("addresses") or [])), 2)
    return {
        "entity_id": entity_id,
        "entity_label": label,
        "psa_id": summary.get("psa_id"),
        "name_match": round(max(name_match(n, label) for n in legal_names), 2),
        "exact_legal_name": any(is_exact(n, label, address_match) for n in legal_names),
        "address_match": address_match,
        "data_volume": data_volume(summary),
        "entity_address": (summary.get("addresses") or [""])[0][:80],
        "sayari_url": summary.get("entity_url"),  # an API path (/entity/<id>); the ID finds the record in Sayari's app
        "risk_flags": len(summary.get("risk") or {}),
    }


def pick(candidates):
    """Best candidate. Exact legal-name matches win outright; among them the record Sayari knows most about
    wins (address only breaks near-ties), because sister companies share HQ addresses and thin duplicates
    often carry the cleanest address. Without an exact match, rank on name + address."""
    exact = [c for c in candidates if c["exact_legal_name"]]
    if exact:
        return max(exact, key=lambda c: (math.log10(1 + c["data_volume"]) + 0.5 * c["address_match"]))
    return max(candidates, key=lambda c: 5 * c["name_match"] + 3 * c["address_match"])


def fullest_in_group(best, candidates):
    """Among candidates in the same psa_id group as `best`, the one with the most data."""
    if not best["psa_id"]:
        return best
    return max((c for c in candidates if c["psa_id"] == best["psa_id"]), key=lambda c: c["data_volume"])


def hand_checked(path=ROOT / "data" / "list_3_hand_checked.tsv"):
    """name -> (record IDs, first one primary; verdict) from the hand review of the weaker matches."""
    if not Path(path).exists():
        return {}
    table = pd.read_csv(path, sep="\t")
    return {r.name: (r.records.split("|"), r.verdict) for r in table.itertuples()}


def confidence(evidence):
    """Judged on the record actually used (after the psa_id switch), not the one that won the search."""
    if evidence["exact_legal_name"] and evidence["address_match"] >= 0.5:
        return "high"
    if evidence["exact_legal_name"] or (evidence["name_match"] >= 0.5 and evidence["address_match"] >= 0.5):
        return "medium"
    return "low - review"


def main(list_path=ROOT / "data" / "list_3.tsv", names_path=ROOT / "data" / "list_3_legal_names.tsv"):
    suppliers = pd.read_csv(list_path, sep="\t").merge(pd.read_csv(names_path, sep="\t"), on="name", how="left")
    checked = hand_checked()
    rows = []
    for s in suppliers.itertuples():
        # legal_name may list several names separated by "|" (current and former).
        legal_names = s.legal_name.split("|") if isinstance(s.legal_name, str) else [s.name]
        print(f"{s.name} ...")
        found = search(dict.fromkeys(legal_names + [s.name]), s.address, s.country)
        base = {"input_name": s.name, "legal_name": " | ".join(legal_names), "input_address": s.address,
                "country": s.country}
        candidates = [evidence_for(c["entity_id"], legal_names, s.address) for c in found]
        if s.name in checked:  # the hand check wins, even if today's search found nothing
            records, verdict = checked[s.name]
            chosen = evidence_for(records[0], legal_names, s.address)
            extra = records[1:]
            label = f"checked by hand: {verdict}"
        elif not candidates:
            rows.append({**base, "confidence": "no match"})
            continue
        else:
            chosen = fullest_in_group(pick(candidates), candidates)
            extra, label = [], confidence(chosen)
        # Other records with the exact legal name: the same company, split across records Sayari doesn't group.
        extra += [c["entity_id"] for c in candidates
                  if c["exact_legal_name"] and c["entity_id"] != chosen["entity_id"] and c["entity_id"] not in extra]
        volume = chosen["data_volume"] + sum(data_volume(profile(e)) for e in extra)
        rows.append({**base, **chosen, "other_records": "|".join(extra), "total_data_volume": volume,
                     "confidence": label, "candidates_checked": len(candidates)})
    out = pd.DataFrame(rows)
    out.to_csv(OUTPUT / "matches.csv", index=False)
    print(out["confidence"].value_counts().to_string())


if __name__ == "__main__":
    main()
