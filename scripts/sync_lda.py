"""Download federal lobbying reports (LD-2) from lda.gov and build data/lobbying.json.

Covers the last four quarters whose filing deadline has passed. Uses only the
standard library and needs no API key. Without a key lda.gov allows about 15
requests a minute, so the first full download takes about four hours. A compact
cache of every report is kept between runs (GitHub Actions cache), and later
runs only ask for reports posted since the last one. If a run hits its time
budget it saves where it got to and the next run picks up from there.
An optional LDA_API_KEY secret makes the first download much faster.
"""
import datetime as dt
import json
import threading
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("LDA_API_KEY", "").strip()
BASE = os.environ.get("LDA_API_BASE", "https://lda.gov/api/v1").rstrip("/")
PAUSE = float(os.environ.get("REQUEST_PAUSE", "0.55" if KEY else "4.2"))
MIN_FILINGS = int(os.environ.get("MIN_FILINGS", "40000"))
CACHE = os.environ.get("LDA_CACHE", ".lda-cache/cache.json")
BUDGET = float(os.environ.get("TIME_BUDGET_MIN", "300")) * 60
STARTED = time.monotonic()
OUT = os.environ.get("LDA_OUT", "data/lobbying.json")
TODAY = dt.date.fromisoformat(os.environ["LDA_TODAY"]) if os.environ.get("LDA_TODAY") else dt.date.today()
requests_made = 0
_lock = threading.Lock()
_next_slot = [0.0]


def wait_turn():
    """Space request starts PAUSE seconds apart across all threads, staying under the rate limit."""
    global requests_made
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + PAUSE
        requests_made += 1
    time.sleep(max(0, slot - now))


def get(url):
    headers = {"Accept": "application/json", "User-Agent": "followthemoney-lobbying-sync"}
    if KEY:
        headers["Authorization"] = f"Token {KEY}"
    for attempt in range(8):
        wait_turn()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(f"lda.gov refused the request (HTTP {e.code}). Check the LDA_API_KEY secret.")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 60) + 2
                print(f"Rate limited. Waiting {wait}s.", flush=True)
                with _lock:
                    _next_slot[0] = max(_next_slot[0], time.monotonic() + min(wait, 900))
            elif e.code >= 500:
                time.sleep(15 * (attempt + 1))
            else:
                raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"Temporary failure ({type(e).__name__}). Retrying.", flush=True)
            time.sleep(15 * (attempt + 1))
    raise RuntimeError("lda.gov kept failing after retries")


def quarters():
    """The last four quarters whose reports were due (20 days after quarter end)."""
    out, y, q = [], TODAY.year, (TODAY.month - 1) // 3 + 1
    while len(out) < 4:
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        end = dt.date(y, q * 3, 30 if q in (2, 3) else 31)
        if TODAY >= end + dt.timedelta(days=21):
            out.append((y, q))
    return sorted(out)


def page_url(year, ftype, page, since=None):
    params = {"filing_year": year, "filing_type": ftype, "ordering": "dt_posted", "page_size": 25, "page": page}
    if since:
        params["filing_dt_posted_after"] = since
    return f"{BASE}/filings/?{urllib.parse.urlencode(params)}"


def out_of_time():
    return time.monotonic() - STARTED > BUDGET


def load_cache():
    try:
        with open(CACHE) as fh:
            c = json.load(fh)
        if c.get("v") == 1:
            return c
    except (OSError, ValueError):
        pass
    return {"v": 1, "progress": {}, "regs": {}, "clients": {}, "lobs": {}, "issues": [], "recs": {}}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(c, fh, separators=(",", ":"))
    os.replace(tmp, CACHE)


def ingest(c, f, y, q):
    """Keep only what the site needs from one report. Newer postings replace older ones (amendments)."""
    reg, cl = f.get("registrant") or {}, f.get("client") or {}
    rid, cid = str(reg.get("id")), str(cl.get("id"))
    key = f"{rid}|{cid}|{y}|{q}"
    posted = f.get("dt_posted") or ""
    old = c["recs"].get(key)
    if old and old[0] >= posted:
        return posted
    c["regs"][rid] = [reg.get("name") or "", reg.get("state") or "", " ".join((reg.get("description") or "").split())[:160]]
    c["clients"][cid] = [cl.get("name") or "", " ".join((cl.get("general_description") or "").split())[:120],
                         cl.get("state") or "", 1 if cl.get("client_self_select") else 0]
    issues, lobs = [], []
    for act in f.get("lobbying_activities") or []:
        issue = act.get("general_issue_code_display") or act.get("general_issue_code")
        if issue:
            if issue not in c["issues"]:
                c["issues"].append(issue)
            issues.append(c["issues"].index(issue))
        for row in act.get("lobbyists") or []:
            lb = row.get("lobbyist") or {}
            if not lb.get("id"):
                continue
            lid = str(lb["id"])
            entry = c["lobs"].setdefault(lid, [person(lb), ""])
            pos = " ".join((row.get("covered_position") or "").split())
            if pos and pos.upper() not in ("N/A", "NA", "NONE") and len(pos) > len(entry[1]):
                entry[1] = pos[:220]
            if lid not in lobs:
                lobs.append(lid)
    c["recs"][key] = [posted, num(f.get("income")), num(f.get("expenses")), sorted(set(issues)), lobs]
    return posted


def sync_type(c, y, q, ftype):
    """Bring one report type up to date. Returns False if the time budget ran out first."""
    pk = f"{y}|{ftype}"
    prog = c["progress"].setdefault(pk, {"page": 1, "done": False, "since": ""})
    if prog["done"]:
        # Only reports posted since the last run, starting two days back to be safe.
        since = (dt.date.fromisoformat(prog["since"][:10]) - dt.timedelta(days=2)).isoformat() if prog["since"] else None
        page = 1
        while True:
            data = get(page_url(y, ftype, page, since))
            for f in data.get("results", []):
                prog["since"] = max(prog["since"], ingest(c, f, y, q))
            if not data.get("next"):
                return True
            page += 1
    while True:
        data = get(page_url(y, ftype, prog["page"]))
        for f in data.get("results", []):
            prog["since"] = max(prog["since"], ingest(c, f, y, q))
        if prog["page"] % 50 == 0:
            print(f"    {y} {ftype}: page {prog['page']} of {-(-data.get('count', 0) // 25)}", flush=True)
            save_cache(c)
        if not data.get("next"):
            prog["done"] = True
            return True
        prog["page"] += 1
        if out_of_time():
            return False


NOT_ACRONYM = {"MR", "MRS", "MS", "DR", "ST", "JR", "SR", "MT", "FT", "MSGR"}
DOTTED = re.compile(r"^(?:[A-Za-z]\.){2,}[A-Za-z]?\.?$")


def acronym(part, people=False):
    """True for parts like CTIA, U.S.A., RPLCC (no vowels) that should stay in capitals."""
    bare = part.strip(",.()\"'")
    if not bare:
        return False
    up = bare.upper()
    if up in UPPER or DOTTED.match(bare):
        return True
    return not people and bare.isalpha() and 2 <= len(bare) <= 5 and not re.search(r"[AEIOUY]", up) and up not in NOT_ACRONYM


SMALL = {"of", "and", "the", "for", "on", "in", "at", "to", "a", "an", "by"}
UPPER = {"LLC", "LLP", "PLLC", "PC", "PA", "LP", "USA", "US", "U.S.", "DC", "D.C.", "II", "III", "IV", "PAC",
         "AFL-CIO", "AARP", "NRA", "IBM", "AT&T", "3M", "BGR", "CGCN", "FTI", "K&L", "LNG", "AI", "HHS", "DOD", "NASA",
         "JP", "TP", "UPS", "USAA", "AIPAC", "CTA", "SEIU", "AFSCME", "AMA", "AHA", "NAR", "NAM", "NFIB", "CVS", "GE", "BP", "MITRE", "SIFMA", "PhRMA", "NCTA", "CTIA", "AHIP", "ACLU", "NAACP", "NEA"}


def cap(p):
    """Capitalize the first letter, skipping leading punctuation like ( or "."""
    m = re.search(r"[A-Za-z0-9]", p)
    return p if not m else p[:m.start()] + p[m.start()].upper() + p[m.start() + 1:].lower()


def title(s, people=False):
    s = " ".join((s or "").split()).rstrip(" ,")
    if not s:
        return ""
    if s != s.upper() and s != s.lower():
        return s  # already mixed case
    words = []
    for i, w in enumerate(s.split(" ")):
        bare = w.strip(",.()\"'")
        if acronym(w, people) or (len(bare) <= 3 and "." in bare) or ("&" in bare and len(bare) <= 5):
            words.append(w.upper().replace("PHRMA", "PhRMA"))
        elif i and w.lower() in SMALL:
            words.append(w.lower())
        else:
            words.append("-".join(p.upper() if acronym(p, people) else cap(p) for p in w.split("-")))
    out = " ".join(words)
    out = re.sub(r"\b([OD])'([a-z])", lambda m: m.group(1) + "'" + m.group(2).upper(), out)
    out = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), out)
    out = re.sub(r"(\w)'S\b", r"\1's", out)
    return out


def person(l):
    first = l.get("nickname") or l.get("first_name") or ""
    parts = [first, l.get("last_name") or ""]
    suffix = l.get("suffix_display") or l.get("suffix") or ""
    name = title(" ".join(p for p in parts if p), people=True)
    suffix = title(suffix, people=True)
    return f"{name} {suffix}".strip() if suffix and suffix.upper() not in ("NONE",) else name


def client_key(name):
    """Group the same client across firms: drop punctuation and legal suffixes."""
    n = (name or "").upper()
    n = n.split(" ON BEHALF OF ")[-1]
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^A-Z0-9& ]", " ", n)
    n = re.sub(r"\b(THE|INC|INCORPORATED|LLC|LLP|L L C|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LP|NA|N A|US|USA|AND)\b", " ", n)
    return " ".join(n.split())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    qs = quarters()
    print("Quarters: " + ", ".join(f"Q{q} {y}" for y, q in qs) + ("" if KEY else " (no key, about 15 requests a minute)"), flush=True)
    c = load_cache()
    keep_years = {str(y) for y, _ in qs}
    window = {f"{y}|{q}" for y, q in qs}
    c["recs"] = {k: v for k, v in c["recs"].items() if "|".join(k.split("|")[2:]) in window}
    c["progress"] = {k: v for k, v in c["progress"].items() if k.split("|")[0] in keep_years}
    sets = [(y, q, t) for y, q in qs for t in (f"Q{q}", f"{q}A", f"{q}T", f"{q}@")]
    # Unfinished downloads first, so a short run always makes progress; then catch up on new reports.
    sets.sort(key=lambda s: c["progress"].get(f"{s[0]}|{s[2]}", {}).get("done", False))
    finished = True
    for y, q, ftype in sets:
        if not sync_type(c, y, q, ftype):
            finished = False
            break
        print(f"  {y} {ftype}: up to date ({requests_made} requests this run)", flush=True)
    save_cache(c)
    if not finished:
        done = sum(1 for y, q, t in sets if c["progress"].get(f"{y}|{t}", {}).get("done"))
        print(f"Time budget reached. {done} of 16 report sets are loaded; the next run continues from here.", flush=True)
        return
    if len(c["recs"]) < MIN_FILINGS:
        sys.exit(f"Only {len(c['recs'])} reports are cached (expected at least {MIN_FILINGS}). Keeping the last good data.")
    build(c, qs)


SUSPECT = 5_000_000  # largest real single-client quarterly fees are around $1M


def build(c, qs):
    qlabels = [f"{y}-Q{q}" for y, q in qs]
    firms, clients, lobbyists = {}, {}, {}
    lob_index, client_index = {}, {}
    for key, (posted, inc, exp, issues, lobs) in c["recs"].items():
        rid, cid, y, q = key.split("|")
        rname, rst, rdesc = c["regs"][rid]
        cname, cdesc, cst, self_select = c["clients"][cid]
        qi = qlabels.index(f"{y}-Q{q}")
        in_house = bool(self_select) or (inc is None and exp is not None)
        amount = (inc if inc is not None else exp) or 0
        firm = firms.setdefault(rid, {"id": int(rid), "n": title(rname), "st": rst, "d": rdesc,
                                      "own": 0, "paid": 0, "q": [0] * len(qs), "c": {}, "l": {}, "i": {},
                                      "under": 0, "odd": 0})
        if not in_house and amount > SUSPECT:
            firm["odd"] += 1  # one client paying one firm this much in a quarter is almost certainly a filing error
            amount = 0
        firm["own" if in_house else "paid"] += amount
        firm["q"][qi] += amount
        if inc is None and exp is None:
            firm["under"] += 1
        sub = " ON BEHALF OF " in cname.upper()
        ck = client_key(cname)
        if ck not in client_index:
            client_index[ck] = len(clients)
            clients[ck] = [title(re.split(r"(?i) on behalf of ", cname)[-1]), cdesc, cst]
        ci = client_index[ck]
        slot = firm["c"].setdefault(ci, [ci, 0, 0, 0])  # client, amount, subcontract flag, in-house flag
        slot[1] += amount
        slot[2] = slot[2] or int(sub)
        slot[3] = slot[3] or int(in_house)
        for i in issues:
            name = c["issues"][i]
            firm["i"][name] = firm["i"].get(name, 0) + 1
        for lid in lobs:
            if lid not in lob_index:
                lob_index[lid] = len(lobbyists)
                lobbyists[lid] = c["lobs"][lid]
            firm["l"][lob_index[lid]] = 1

    out_firms = []
    for f in firms.values():
        total = round(sum(f["q"]))
        out_firms.append({
            "id": f["id"], "n": f["n"], "st": f["st"], "d": f["d"], "h": int(f["own"] > f["paid"]), "t": total,
            "q": [round(x) for x in f["q"]],
            "c": sorted([[c, round(a), s, h] for c, a, s, h in f["c"].values()], key=lambda x: -x[1]),
            "l": sorted(f["l"].keys()),
            "i": [k for k, _ in sorted(f["i"].items(), key=lambda x: -x[1])[:6]],
            "u": f["under"],
            **({"x": f["odd"]} if f["odd"] else {}),
        })
    out_firms.sort(key=lambda x: -x["t"])
    data = {
        "meta": {"generated": TODAY.isoformat(), "quarters": qlabels, "filings": len(c["recs"]),
                 "source": "lda.gov", "requests": requests_made},
        "firms": out_firms,
        "clients": list(clients.values()),
        "lobbyists": list(lobbyists.values()),
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    outside = sum(1 for f in out_firms if not f["h"])
    print(f"Saved {len(out_firms)} registrants ({outside} outside firms), {len(clients)} clients, "
          f"{len(lobbyists)} lobbyists from {len(c["recs"])} reports using {requests_made} requests.", flush=True)


if __name__ == "__main__":
    main()
