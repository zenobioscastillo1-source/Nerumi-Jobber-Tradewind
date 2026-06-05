"""Demo harness (lives in demo/, not a production tool).

Simulates a Layer 1 ingest run against a fake in-memory "Jobber account" so we can see the
detection rules + Sheet surfacing without real sandbox data. Uses the REAL tools/ code for
completeness, duplicate matching, and the Sheet upsert; only the live Jobber `clients` HTTP
search is stubbed to search the fixture set (that query is already verified schema-valid live).

Writes DEMO-prefixed rows to the real Sheet, runs a second pass to prove dedup, then dumps both
tabs. Safe to delete the rows afterward.
"""
import json
import subprocess
import sys
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import hygiene_rules as H              # noqa: E402
import find_duplicate_clients as F    # noqa: E402
from jobber_queries import format_address  # noqa: E402
from google_auth import sheets_service     # noqa: E402
from sheets_io import get_spreadsheet_id, quote_tab  # noqa: E402
from sheet_schema import REQUESTS_COLUMNS, HYGIENE_COLUMNS  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def C(cid, name, first, last, company, emails, phones, addr):
    return {"id": cid, "name": name, "first_name": first, "last_name": last,
            "company_name": company, "emails": emails, "phones": phones,
            "address": addr, "address_str": format_address(addr)}


# --- the fake "Jobber account": 7 clients ---
ACCOUNT = [
    C("CLI-DEMO-A", "Maria Gonzalez", "Maria", "Gonzalez", "",
      ["maria.gonzalez@example.com"], ["(512) 555-0101"],
      {"street1": "412 Pecan St", "city": "Austin", "province": "TX", "postalCode": "78701", "country": "USA"}),
    C("CLI-DEMO-B", "Cedar Ridge Property Mgmt", "", "", "Cedar Ridge Property Mgmt",
      ["office@cedarridgepm.example"], ["512-555-0202"],
      {"street1": "88 Commerce Blvd", "city": "Austin", "province": "TX", "postalCode": "78745", "country": "USA"}),
    # strong duplicate pair — shared phone, different email
    C("CLI-DEMO-C1", "John Smith", "John", "Smith", "",
      ["john.smith@example.com"], ["(512) 555-0303"],
      {"street1": "100 Oak St", "city": "Round Rock", "province": "TX", "postalCode": "78664", "country": "USA"}),
    C("CLI-DEMO-C2", "J. Smith", "J.", "Smith", "",
      ["jsmith.work@example.com"], ["512.555.0303"],
      {"street1": "100 Oak Street", "city": "Round Rock", "province": "TX", "postalCode": "78664", "country": "USA"}),
    # possible duplicate pair — same name + address, different contact info
    C("CLI-DEMO-D1", "Linda Park", "Linda", "Park", "",
      ["linda.park@example.com"], ["(512) 555-0404"],
      {"street1": "250 Maple Ave", "city": "Austin", "province": "TX", "postalCode": "78704", "country": "USA"}),
    C("CLI-DEMO-D2", "Linda Park", "Linda", "Park", "",
      ["lpark2024@example.net"], ["(512) 555-9999"],
      {"street1": "250 Maple Ave", "city": "Austin", "province": "TX", "postalCode": "78704", "country": "USA"}),
    # incomplete — no email, no phone, no street
    C("CLI-DEMO-E", "Carlos Rivera", "Carlos", "Rivera", "",
      [], [], {"city": "Austin", "province": "TX"}),
]
BY_ID = {c["id"]: c for c in ACCOUNT}

# one inbound Request per client
REQUESTS = [
    {"request_id": f"REQ-DEMO-{i+1:03d}", "created_at": f"2026-05-31T1{i}:00:00Z",
     "title": t, "source": s, "client": BY_ID[cid]}
    for i, (cid, t, s) in enumerate([
        ("CLI-DEMO-A", "Spring flower bed and garden install", "Website"),
        ("CLI-DEMO-B", "Quarterly grounds maintenance contract", "Phone"),
        ("CLI-DEMO-C1", "Sprinkler / irrigation system install", "Website"),
        ("CLI-DEMO-C2", "Sprinkler install follow-up", "Phone"),
        ("CLI-DEMO-D1", "Full landscape design consultation", "Referral"),
        ("CLI-DEMO-D2", "Hedge trimming and fall cleanup", "Website"),
        ("CLI-DEMO-E", "Emergency storm cleanup and tree removal", "Phone"),
    ])
]


def fake_search(term, first):
    """Stand in for the live Jobber clients(searchTerm:) call: search the fixture account."""
    t = term.strip().lower()
    out = []
    for c in ACCOUNT:
        hay = " ".join([c["name"], *c["emails"], *c["phones"]]).lower()
        if t and t in hay:
            out.append(c)
        if len(out) >= first:
            break
    return out


F._search = fake_search  # stub only the network; real find_duplicates logic runs


def assess(c):
    comp = H.completeness(c)
    dup = F.find_duplicates(c, 10)
    sig = dup["signal"]
    exact_ids = [m["client_id"] for m in dup["exact_matches"]]
    fuzzy_ids = [m["client_id"] for m in dup["fuzzy_candidates"]]
    dup_of = "; ".join(exact_ids + fuzzy_ids)
    matched_on = sorted({mo for m in dup["exact_matches"] for mo in m["matched_on"]})
    if sig == "strong":
        reason = f"Shares {', '.join(matched_on)} with {', '.join(exact_ids)}"
    elif sig == "possible":
        reason = f"Same name & address as {', '.join(fuzzy_ids)} (different contact info)"
    else:
        reason = ""
    return comp, sig, dup_of, reason


def upsert(target, row):
    p = ROOT / ".tmp" / f"_row_{target}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(row), encoding="utf-8")
    r = subprocess.run([sys.executable, str(TOOLS / "log_to_sheet.py"),
                        "--target", target, "--row-file", str(p)],
                       capture_output=True, text=True, cwd=str(ROOT))
    return (r.stdout + r.stderr).strip()


def run_pass(label):
    print(f"\n===== {label} =====")
    for req in REQUESTS:
        c = req["client"]
        comp, sig, dup_of, reason = assess(c)
        flagged = (sig != "none") or comp["incomplete"]
        req_row = {
            "request_id": req["request_id"], "created_at": req["created_at"],
            "request_title": req["title"], "request_source": req["source"],
            "client_id": c["id"], "client_name": c["name"],
            "client_emails": "; ".join(c["emails"]), "client_phones": "; ".join(c["phones"]),
            "client_address": c["address_str"], "duplicate_flag": sig,
            "duplicate_of": dup_of, "duplicate_reason": reason,
            "incomplete_flag": "yes" if comp["incomplete"] else "no",
            "missing_fields": "; ".join(comp["missing_fields"]),
            "status": "needs_review" if flagged else "surfaced",
            "surfaced_at": NOW, "owner_notes": "SYNTHETIC DEMO DATA - safe to delete",
        }
        out_r = upsert("requests", req_row)
        out_h = ""
        if flagged:
            issue = "both" if (sig != "none" and comp["incomplete"]) else ("duplicate" if sig != "none" else "incomplete")
            hyg_row = {
                "client_id": c["id"], "client_name": c["name"],
                "client_emails": "; ".join(c["emails"]), "client_phones": "; ".join(c["phones"]),
                "client_address": c["address_str"], "issue": issue, "duplicate_flag": sig,
                "duplicate_of": dup_of, "duplicate_reason": reason,
                "missing_fields": "; ".join(comp["missing_fields"]),
                "first_seen_request_id": req["request_id"], "status": "needs_review",
                "surfaced_at": NOW, "owner_action": "",
            }
            out_h = upsert("hygiene", hyg_row)
        print(f"  {req['request_id']} {c['name']:<18} dup={sig:<8} incomplete={req_row['incomplete_flag']:<3}"
              f" | req:{out_r.split()[0]:<9} hyg:{(out_h.split()[0] if out_h else '-')}")


def dump_tab(svc, sid, tab, cols):
    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{quote_tab(tab)}!A1:{chr(64+len(cols))}").execute().get("values", [])
    print(f"\n----- {tab}: {max(0, len(rows)-1)} data row(s) -----")
    show = ["request_id", "client_name", "duplicate_flag", "duplicate_of", "incomplete_flag", "missing_fields"] \
        if tab.lower().startswith("req") else \
        ["client_id", "client_name", "issue", "duplicate_flag", "duplicate_of", "missing_fields"]
    idx = [cols.index(s) for s in show]
    for r in rows[1:]:
        r = r + [""] * (len(cols) - len(r))
        print("   " + " | ".join(str(r[i]) for i in idx))


def main():
    run_pass("PASS 1 (first surfacing)")
    run_pass("PASS 2 (re-run -> expect SKIPPED, dedup)")
    svc, sid = sheets_service(), get_spreadsheet_id()
    dump_tab(svc, sid, "Requests", REQUESTS_COLUMNS)
    dump_tab(svc, sid, "Client Hygiene", HYGIENE_COLUMNS)


if __name__ == "__main__":
    main()
