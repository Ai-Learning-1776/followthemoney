#!/usr/bin/env python3
"""Pull federal candidate finance data from OpenFEC into data/candidates.json.

Standard library only. The key is read from FEC_API_KEY, sent in a header, and
never printed. If a run looks incomplete, the last good data file is left alone.

Only committee contributors are saved. Individual donors are never stored.
"""
import datetime, json, os, pathlib, sys, time, urllib.error, urllib.parse, urllib.request

BASE = os.environ.get("FEC_API_BASE", "https://api.open.fec.gov/v1").rstrip("/")
KEY = os.environ.get("FEC_API_KEY", "")
CYCLE = int(os.environ.get("FEC_CYCLE", "2026"))
MIN_RECEIPTS = float(os.environ.get("MIN_RECEIPTS", "5000"))
TOP_COMMITTEE_LOOKUPS = int(os.environ.get("TOP_COMMITTEE_LOOKUPS", "400"))
MIN_EXPECTED = int(os.environ.get("MIN_EXPECTED", "300"))
PAUSE = float(os.environ.get("REQUEST_PAUSE", "3.7"))  # stays under 1,000 requests an hour
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "candidates.json"
OVERRIDES = json.loads((ROOT / "scripts" / "name_overrides.json").read_text())
INDEPENDENT = {"IND", "NPA", "NNE", "NOP"}
SUFFIXES = {"JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "DR", "SEN", "REP", "HON", "ESQ", "PHD", "MD"}
requests_made = 0


def get(path, params):
    """GET one API page with retries. The key stays out of the URL and the logs."""
    global requests_made
    url = f"{BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(6):
        time.sleep(PAUSE)
        requests_made += 1
        req = urllib.request.Request(url, headers={"X-Api-Key": KEY, "User-Agent": "followthemoney-sync"})
        try:
            with urllib.request.urlopen(req, timeout=90) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(f"The FEC service refused the key (HTTP {e.code}). Check the FEC_API_KEY secret.")
            if e.code == 429:
                print("Rate limit reached. Waiting 10 minutes.", flush=True)
                time.sleep(600)
            elif e.code >= 500:
                time.sleep(20 * (attempt + 1))
            else:
                raise RuntimeError(f"{path} returned HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"{path}: temporary failure ({type(e).__name__}). Retrying.", flush=True)
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"{path} kept failing after retries")


def pages(path, params):
    page = 1
    while True:
        data = get(path, {**params, "page": page, "per_page": 100})
        yield from data.get("results", [])
        if page >= data.get("pagination", {}).get("pages", 1):
            return
        page += 1


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def pretty_name(filed):
    if "," not in filed:
        return filed.title()
    last, rest = filed.split(",", 1)
    words = [w for w in rest.replace(".", " ").split() if w.upper() not in SUFFIXES]
    fix = lambda w: "-".join(p.capitalize() for p in w.split("-"))
    def case(w):
        w = fix(w)
        if w.startswith("Mc") and len(w) > 2:
            return "Mc" + w[2:].capitalize()
        if w.startswith("O'") and len(w) > 2:
            return "O'" + w[2:].capitalize()
        return w
    first = [case(w) for w in words[:1]]
    surname = [case(w) for w in last.split() if w.upper() not in SUFFIXES]
    return " ".join(first + surname)


def fetch_candidates():
    rows, seen = [], set()
    for office in ("S", "H"):
        kept = 0
        for r in pages("/candidates/totals/", {"cycle": CYCLE, "election_full": "false", "office": office,
                                               "sort": "-receipts", "sort_hide_null": "true"}):
            if num(r.get("receipts")) < MIN_RECEIPTS:
                break
            if r["candidate_id"] not in seen:
                seen.add(r["candidate_id"])
                rows.append(r)
                kept += 1
        print(f"{office}: {kept} candidates", flush=True)
    return rows


def fetch_committees(ids):
    found = {}
    for batch in chunks(ids, 100):
        for c in pages("/candidates/search/", {"candidate_id": batch}):
            principal = [m for m in c.get("principal_committees") or [] if m.get("designation") == "P"]
            match = next((m for m in principal if CYCLE in (m.get("cycles") or [])), principal[0] if principal else None)
            if match:
                found[c["candidate_id"]] = match["committee_id"]
    return found


def fetch_totals(committee_ids):
    totals = {}
    for batch in chunks(committee_ids, 100):
        for t in pages("/totals/house-senate/", {"cycle": CYCLE, "committee_id": batch}):
            totals[t["committee_id"]] = t
    return totals


def fetch_reports(committee_ids):
    reports = {}
    for batch in chunks(committee_ids, 40):
        for r in pages("/reports/house-senate/", {"cycle": CYCLE, "is_amended": "false", "committee_id": batch,
                                                 "sort": "-coverage_end_date"}):
            end, start = (r.get("coverage_end_date") or "")[:10], (r.get("coverage_start_date") or "")[:10]
            if end:
                reports.setdefault(r["committee_id"], []).append(
                    [r.get("report_type") or "", start, end, round(num(r.get("total_receipts_period")))])
    return reports


def fetch_top_committees(committee_id):
    """Largest committee contributions. Individuals and candidates are excluded."""
    data = get("/schedules/schedule_a/", {"committee_id": committee_id, "two_year_transaction_period": CYCLE,
                                          "is_individual": "false", "sort": "-contribution_receipt_amount",
                                          "per_page": 100})
    totals = {}
    for row in data.get("results", []):
        if row.get("memo_code") == "X" or row.get("entity_type") in ("IND", "CAN"):
            continue
        name = " ".join((row.get("contributor_name") or "").split())
        amount = num(row.get("contribution_receipt_amount"))
        if name and amount > 0:
            totals[name] = totals.get(name, 0) + amount
    return [[n, round(a)] for n, a in sorted(totals.items(), key=lambda x: -x[1])[:10]]


def main():
    if not KEY:
        sys.exit("FEC_API_KEY is missing. Add it under Settings > Secrets and variables > Actions.")
    rows = fetch_candidates()
    committees = fetch_committees([r["candidate_id"] for r in rows])
    # One committee can appear under two candidate IDs. Keep the higher-ranked row.
    seen_cm, kept = set(), []
    for r in rows:
        cm = committees.get(r["candidate_id"])
        if cm and cm not in seen_cm:
            seen_cm.add(cm)
            kept.append(r)
    cm_ids = [committees[r["candidate_id"]] for r in kept]
    totals = fetch_totals(cm_ids)
    print(f"Totals loaded: {len(totals)}", flush=True)
    reports = fetch_reports(cm_ids)
    print(f"Report histories loaded: {len(reports)}", flush=True)

    top = {}
    failures = 0
    for r in sorted(kept, key=lambda x: -num(x.get("receipts")))[:TOP_COMMITTEE_LOOKUPS]:
        cm = committees[r["candidate_id"]]
        try:
            top[cm] = fetch_top_committees(cm)
        except RuntimeError as e:
            failures += 1
            print(f"Contributor lookup skipped for {cm}: {e}", flush=True)
    print(f"Contributor lists loaded: {len(top)}; skipped: {failures}", flush=True)

    out = []
    for r in kept:
        cid = r["candidate_id"]
        cm = committees[cid]
        t = totals.get(cm, {})
        party = r.get("party") or ""
        party = party if party in ("DEM", "REP") else "IND" if party in INDEPENDENT else "OTH"
        filed = " ".join((r.get("name") or "").split())
        out.append({
            "id": cid, "f": filed, "n": OVERRIDES.get(cid) or pretty_name(filed), "p": party, "pf": r.get("party_full") or "",
            "o": r.get("office"), "st": r.get("state") or "", "dist": r.get("district") or "00",
            "ic": r.get("incumbent_challenge") or "", "r": round(num(r.get("receipts"))),
            "s": round(num(r.get("disbursements"))), "c": round(num(r.get("cash_on_hand_end_period"))),
            "thru": (r.get("coverage_end_date") or "")[:10], "cm": cm,
            "b": {"ii": round(num(t.get("individual_itemized_contributions"))),
                  "iu": round(num(t.get("individual_unitemized_contributions"))),
                  "pac": round(num(t.get("other_political_committee_contributions"))),
                  "pty": round(num(t.get("political_party_committee_contributions"))),
                  "own": round(num(t.get("candidate_contribution")) + num(t.get("loans_made_by_candidate"))),
                  "tr": round(num(t.get("transfers_from_other_authorized_committee")))},
            "rep": reports.get(cm, []), "top": top.get(cm, []),
        })

    if len(out) < MIN_EXPECTED or len(totals) < 0.9 * len(out):
        sys.exit(f"Run looks incomplete ({len(out)} candidates, {len(totals)} totals). Keeping the last good data.")
    meta = {"generated": datetime.date.today().isoformat(), "cycle": CYCLE, "count": len(out), "sample": False,
            "source": "OpenFEC", "minReceipts": MIN_RECEIPTS, "requests": requests_made}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"meta": meta, "candidates": out}, separators=(",", ":")))
    tmp.replace(OUT)
    print(f"Saved {len(out)} candidates using {requests_made} requests.", flush=True)


if __name__ == "__main__":
    main()
