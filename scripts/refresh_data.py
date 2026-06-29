#!/usr/bin/env python3
"""
Pulls fresh deal data from HubSpot and rewrites the DEALS array in index.html.
Run by GitHub Actions daily, or locally: python3 scripts/refresh_data.py
Requires: HUBSPOT_API_KEY env var
"""

import json, os, re, sys, urllib.request, concurrent.futures
from datetime import datetime, timezone

API_KEY  = os.environ.get("HUBSPOT_API_KEY", "")
PIPELINE = "9204511"
STAGE_MAP = {
    "28706170":   "ieo",
    "26189649":   "qeo",
    "1028889990": "jda",
    "26189651":   "bip",
}
OWNER_MAP = {
    "109798973": "stephen",
    "311418551": "alexis",
    "866820848": "maria",
    "637214576": "sara",
}
TODAY = datetime.now(timezone.utc)

# ── helpers ───────────────────────────────────────────────────────────────

def hs_get(path, params=""):
    url = f"https://api.hubapi.com{path}{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def hs_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.hubapi.com{path}", data=data,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def days_ago(s):
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        v = (TODAY - dt).days
        return v if v >= 0 else None
    except:
        return None

def js_val(v):
    return "null" if v is None else str(v)

def js_str(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')

# ── fetch all open deals ──────────────────────────────────────────────────

def fetch_stage_deals(stage_id, stage_key):
    deals, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline",  "operator": "EQ", "value": PIPELINE},
                {"propertyName": "dealstage", "operator": "EQ", "value": stage_id},
            ]}],
            "properties": ["dealname", "amount", "hubspot_owner_id",
                           "notes_last_contacted", "clickup_task_status"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        resp = hs_post("/crm/v3/objects/deals/search", body)
        deals.extend(resp.get("results", []))
        after = resp.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return stage_key, deals

# ── fetch stage history (days in current stage) ───────────────────────────

def fetch_dis(deal_id, current_stage_id):
    try:
        data = hs_get(f"/crm/v3/objects/deals/{deal_id}",
                      "?propertiesWithHistory=dealstage")
        hist = data.get("propertiesWithHistory", {}).get("dealstage", [])
        for entry in hist:
            if entry["value"] == current_stage_id:
                return days_ago(entry["timestamp"])
    except:
        pass
    return None

# ── fetch contact last touch ──────────────────────────────────────────────

def fetch_dcc(deal_id):
    try:
        assoc = hs_get(f"/crm/v3/objects/deals/{deal_id}/associations/contacts",
                       "?limit=5")
        contacts = [r["id"] for r in assoc.get("results", [])]
        most_recent = None
        for cid in contacts:
            try:
                p = hs_get(f"/crm/v3/objects/contacts/{cid}",
                           "?properties=notes_last_contacted")
                val = p.get("properties", {}).get("notes_last_contacted")
                d = days_ago(val)
                if d is not None and (most_recent is None or d < most_recent):
                    most_recent = d
            except:
                pass
        return most_recent
    except:
        return None

# ── stage ID reverse lookup ───────────────────────────────────────────────

STAGE_ID_LOOKUP = {v: k for k, v in STAGE_MAP.items()}

# ── main ─────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: HUBSPOT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print("Fetching deals by stage...")
    all_raw = []
    for stage_id, stage_key in STAGE_MAP.items():
        key, deals = fetch_stage_deals(stage_id, stage_key)
        print(f"  {key}: {len(deals)}")
        for d in deals:
            props = d["properties"]
            owner_id = props.get("hubspot_owner_id") or ""
            all_raw.append({
                "id":       d["id"],
                "name":     props.get("dealname", ""),
                "stage":    stage_key,
                "stage_id": stage_id,
                "owner":    OWNER_MAP.get(owner_id, "unknown"),
                "amt":      int(float(props["amount"])) if props.get("amount") else 0,
                "ddc":      days_ago(props.get("notes_last_contacted")),
                "cu":       (props.get("clickup_task_status") or "").strip() if stage_key == "bip" else None,
            })

    print(f"Total: {len(all_raw)} deals. Fetching stage history + contact touch...")

    def enrich(deal):
        deal["dis"] = fetch_dis(deal["id"], deal["stage_id"])
        deal["dcc"] = fetch_dcc(deal["id"])
        return deal

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        enriched = list(ex.map(enrich, all_raw))

    print(f"Done. Building DEALS array...")

    lines = []
    for d in enriched:
        cu_field = f',cu:"{js_str(d["cu"])}"' if d["stage"] == "bip" and d["cu"] else ""
        lines.append(
            f'  {{id:"{d["id"]}",name:"{js_str(d["name"])}",stage:"{d["stage"]}",'
            f'owner:"{d["owner"]}",amt:{d["amt"]},'
            f'dis:{js_val(d["dis"])},ddc:{js_val(d["ddc"])},dcc:{js_val(d["dcc"])}{cu_field}}}'
        )

    new_array = "const DEALS = [\n" + ",\n".join(lines) + "\n];"

    # Date string for header
    date_str = TODAY.strftime("%-b %-d, %Y")

    # Read current index.html
    html_path = os.path.join(os.path.dirname(__file__), "..", "index.html")
    with open(html_path) as f:
        html = f.read()

    # Replace DEALS array
    html = re.sub(
        r"const DEALS = \[.*?\];",
        new_array,
        html,
        flags=re.DOTALL
    )

    # Update "As of" date in header
    html = re.sub(
        r"As of [A-Za-z]+ \d+, \d{4}",
        f"As of {date_str}",
        html
    )

    with open(html_path, "w") as f:
        f.write(html)

    total = len(enriched)
    pipeline = sum(d["amt"] for d in enriched)
    print(f"index.html updated: {total} deals, ${pipeline:,} pipeline, as of {date_str}")

if __name__ == "__main__":
    main()
