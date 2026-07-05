#!/usr/bin/env python3
"""Job-alert watcher: polls ToS-permitted job sources and posts new analyst
roles in target locations to a Discord webhook.

Sources:
  - Greenhouse public job-board API (boards-api.greenhouse.io) - permitted for
    automated read access.
  - Lever public postings API (api.lever.co/v0/postings) - permitted for
    automated read access.
  - JP Morgan Chase via Oracle Recruiting Cloud's public candidate-experience
    REST API (the same unauthenticated API their careers site serves).

Never touches LinkedIn or Indeed. Never submits applications.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state", "seen.json")
COMPANIES_PATH = os.path.join(ROOT, "companies.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
# Only notify on postings younger than this; older ones are silently marked
# seen (prevents flooding when a new company is added to the watchlist).
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "72"))
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "12"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
USER_AGENT = "job-alerts-watcher (personal job-search notifier; low frequency)"

TITLE_RE = re.compile(r"analyst", re.I)

# --- Location matching ----------------------------------------------------

GTA_ALWAYS = [
    "toronto", "north york", "scarborough", "etobicoke", "mississauga",
    "brampton", "markham", "vaughan", "richmond hill", "oakville",
    "thornhill", "concord, on",
]
# Ambiguous city names: only count with Ontario/Canada context.
GTA_WITH_CONTEXT = [
    "burlington", "pickering", "ajax", "whitby", "oshawa", "milton",
    "newmarket", "aurora",
]
CANADA_CONTEXT = ["ontario", "canada", ", on", "(on)"]

US_STATE_NAMES = [
    "new york", "california", "oregon", "massachusetts", "arizona",
    "florida", "north carolina",
]
US_ABBREV_RE = re.compile(r"\b(NY|CA|OR|WA|MA|AZ|FL|NC)\b")  # case-sensitive
US_CITIES = [
    "new york city", "nyc", "manhattan", "brooklyn",
    "san francisco", "los angeles", "san diego", "san jose", "sacramento",
    "irvine", "oakland", "palo alto", "mountain view", "menlo park",
    "sunnyvale", "santa monica", "long beach",
    "portland", "seattle", "bellevue", "redmond",
    "boston", "cambridge, ma",
    "phoenix", "scottsdale", "tempe", "tucson",
    "tampa", "miami", "orlando", "jacksonville",
    "charlotte", "raleigh", "durham",
]
DC_RE = re.compile(r"washington,?\s*d\.?c\.?|\bDC\b", re.I)


def match_location(loc):
    """Return 'GTA', 'US', or None for a single location string."""
    if not loc:
        return None
    low = loc.lower()

    has_ca_context = any(c in low for c in CANADA_CONTEXT)
    if any(city in low for city in GTA_ALWAYS):
        return "GTA"
    if has_ca_context and any(city in low for city in GTA_WITH_CONTEXT):
        return "GTA"
    if has_ca_context:
        return None  # elsewhere in Canada

    # Strip DC mentions so "Washington, DC" doesn't read as Washington state.
    stripped = DC_RE.sub("", loc)
    low_stripped = stripped.lower()
    if any(s in low_stripped for s in US_STATE_NAMES):
        return "US"
    if "washington" in low_stripped:
        return "US"  # Washington state (DC already removed)
    if US_ABBREV_RE.search(stripped):
        return "US"
    if any(c in low for c in US_CITIES):
        return "US"
    return None


def classify_locations(locs):
    """Return (matched_locations, region_set) across a list of strings."""
    matched, regions = [], set()
    for loc in locs:
        m = match_location(loc)
        if m:
            matched.append(loc)
            regions.add(m)
    return matched, regions


# --- Work-authorization scan (US postings) --------------------------------

NEG_PATTERNS = [
    "will not sponsor", "unable to sponsor", "does not sponsor",
    "do not sponsor", "cannot sponsor", "can not sponsor",
    "not able to sponsor", "without sponsorship",
    "without the need for sponsorship", "no visa sponsorship",
    "sponsorship is not available", "sponsorship is not offered",
    "not offer sponsorship", "not provide sponsorship",
    "not eligible for visa sponsorship", "not eligible for sponsorship",
    "must be authorized to work", "must be legally authorized",
    "must have authorization to work", "no sponsorship",
]
POS_PATTERNS = [
    "sponsorship available", "sponsorship is available", "will sponsor",
    "able to sponsor", "provides sponsorship", "provide sponsorship for",
    "visa sponsorship offered", "open to sponsorship",
    "sponsorship may be available", "tn visa", "tn status",
]

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    return html.unescape(TAG_RE.sub(" ", text or ""))


def work_auth_flag(description_text):
    low = re.sub(r"\s+", " ", strip_html(description_text)).lower()
    if any(p in low for p in NEG_PATTERNS):
        return "Must be authorized without sponsorship"
    if any(p in low for p in POS_PATTERNS):
        return "Sponsorship/TN visa OK"
    return "No sponsorship mentioned"


# --- HTTP helpers ----------------------------------------------------------

def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# --- Sources ---------------------------------------------------------------

def fetch_greenhouse(token, name):
    """Yield candidate jobs from a Greenhouse board."""
    data = get_json(
        "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % token)
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not TITLE_RE.search(title):
            continue
        loc = (j.get("location") or {}).get("name", "") or ""
        locs = [x.strip() for x in re.split(r"[;|]", loc) if x.strip()] or [loc]
        matched, regions = classify_locations(locs)
        if not regions:
            continue
        posted = parse_iso(j.get("first_published") or j.get("updated_at"))
        yield {
            "key": "greenhouse:%s:%s" % (token, j["id"]),
            "source": "Greenhouse",
            "company": name,
            "title": title,
            "locations": matched,
            "regions": regions,
            "posted": posted,
            "url": j.get("absolute_url", ""),
            "_gh": (token, j["id"]),
            "description": None,  # fetched lazily for US matches
        }


def greenhouse_description(token, job_id):
    try:
        d = get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s"
                     % (token, job_id))
        return d.get("content", "")
    except Exception as e:
        print("  warn: greenhouse detail %s/%s: %s" % (token, job_id, e))
        return ""


def fetch_lever(token, name):
    data = get_json(
        "https://api.lever.co/v0/postings/%s?mode=json" % token)
    for j in data:
        title = j.get("text", "")
        if not TITLE_RE.search(title):
            continue
        cats = j.get("categories") or {}
        locs = list(j.get("allLocations") or [])
        if cats.get("location") and cats["location"] not in locs:
            locs.append(cats["location"])
        matched, regions = classify_locations(locs)
        if not regions:
            continue
        created = j.get("createdAt")
        posted = (datetime.fromtimestamp(created / 1000.0, tz=timezone.utc)
                  if created else None)
        desc = " ".join([
            j.get("descriptionPlain") or "",
            j.get("additionalPlain") or "",
        ] + [(p.get("content") or "") for p in (j.get("lists") or [])
             if isinstance(p, dict)])
        yield {
            "key": "lever:%s:%s" % (token, j["id"]),
            "source": "Lever",
            "company": name,
            "title": title,
            "locations": matched,
            "regions": regions,
            "posted": posted,
            "url": j.get("hostedUrl", ""),
            "description": desc,
        }


JPMC_BASE = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest"
JPMC_SITE = "CX_1001"


def fetch_jpmc():
    url = (JPMC_BASE + "/recruitingCEJobRequisitions?onlyData=true"
           "&expand=requisitionList.secondaryLocations"
           "&finder=findReqs;siteNumber=%s,keyword=analyst,"
           "sortBy=POSTING_DATES_DESC,limit=100,offset=0" % JPMC_SITE)
    data = get_json(url)
    items = data.get("items", [])
    reqs = items[0].get("requisitionList", []) if items else []
    for r in reqs:
        title = r.get("Title", "")
        if not TITLE_RE.search(title):
            continue
        locs = [r.get("PrimaryLocation") or ""]
        locs += [s.get("Name", "") for s in (r.get("secondaryLocations") or [])]
        matched, regions = classify_locations(locs)
        if not regions:
            continue
        # PostedDate is date-only (midnight UTC); credit a full day of slack
        # in the freshness check so day-of postings aren't unfairly aged.
        posted = parse_iso(r.get("PostedDate"))
        rid = r.get("Id")
        yield {
            "key": "jpmc:%s" % rid,
            "source": "JPMC Careers",
            "company": "JPMorgan Chase",
            "title": title,
            "locations": matched,
            "regions": regions,
            "posted": posted,
            "url": ("https://jpmc.fa.oraclecloud.com/hcmUI/"
                    "CandidateExperience/en/sites/%s/job/%s" % (JPMC_SITE, rid)),
            "_jpmc": rid,
            "posted_slack_h": 24,
            "description": None,
        }


def jpmc_description(rid):
    try:
        url = (JPMC_BASE + "/recruitingCEJobRequisitionDetails?onlyData=true"
               "&expand=all&finder=ById;siteNumber=%s,Id=%%22%s%%22"
               % (JPMC_SITE, rid))
        d = get_json(url)
        items = d.get("items", [])
        if not items:
            return ""
        it = items[0]
        return " ".join(str(it.get(k) or "") for k in (
            "ExternalDescriptionStr", "ExternalQualificationsStr",
            "ExternalResponsibilitiesStr", "CorporateDescriptionStr",
            "OrganizationDescriptionStr", "ShortDescriptionStr"))
    except Exception as e:
        print("  warn: jpmc detail %s: %s" % (rid, e))
        return ""


# --- Discord ---------------------------------------------------------------

def post_discord(payload):
    if DRY_RUN:
        print("  DRY RUN, would send: %s" % json.dumps(payload)[:600])
        return 0
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(WEBHOOK, data=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            raise
    return None


def notify(job):
    is_jpmc = job["company"] == "JPMorgan Chase"
    fields = [
        {"name": "Company", "value": job["company"], "inline": True},
        {"name": "Location", "value": "; ".join(job["locations"])[:1000] or "?",
         "inline": True},
        {"name": "Posted", "value": (job["posted"].strftime("%Y-%m-%d %H:%M UTC")
                                     if job["posted"] else "unknown"),
         "inline": True},
    ]
    if "US" in job["regions"]:
        fields.append({"name": "Work authorization (US)",
                       "value": job["work_auth"], "inline": False})
    embed = {
        "title": ("🔥 HIGH PRIORITY — " if is_jpmc else "") + job["title"][:230],
        "url": job["url"],
        "color": 0xE74C3C if is_jpmc else 0x5865F2,
        "fields": fields,
        "footer": {"text": "via %s" % job["source"]},
    }
    payload = {"username": "Job Watch", "embeds": [embed]}
    if is_jpmc:
        payload["content"] = "🔥 **JPMorgan Chase posting — dream employer!**"
    status = post_discord(payload)
    print("  payload: %s" % json.dumps(payload, ensure_ascii=False))
    print("  notified (%s): %s @ %s [%s]"
          % (status, job["title"], job["company"],
             "; ".join(job["locations"])))
    return status


# --- Main ------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=0, sort_keys=True)
        f.write("\n")


def main():
    if not WEBHOOK:
        print("FATAL: DISCORD_WEBHOOK_URL not set")
        return 1
    with open(COMPANIES_PATH) as f:
        companies = json.load(f)

    state = load_state()
    now = now_utc()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    matches = []
    for token, name in companies.get("greenhouse", {}).items():
        try:
            matches.extend(fetch_greenhouse(token, name))
        except Exception as e:
            print("warn: greenhouse %s failed: %s" % (token, e))
    for token, name in companies.get("lever", {}).items():
        try:
            matches.extend(fetch_lever(token, name))
        except Exception as e:
            print("warn: lever %s failed: %s" % (token, e))
    try:
        matches.extend(fetch_jpmc())
    except Exception as e:
        print("warn: jpmc failed: %s" % e)

    print("total matching postings currently live: %d" % len(matches))

    fresh_new = []
    for job in matches:
        if job["key"] in state:
            state[job["key"]]["last_seen"] = now.isoformat()
            continue
        state[job["key"]] = {
            "first_seen": now.isoformat(), "last_seen": now.isoformat(),
            "title": job["title"], "company": job["company"]}
        slack = timedelta(hours=job.get("posted_slack_h", 0))
        if job["posted"] and job["posted"] + slack >= cutoff:
            fresh_new.append(job)
        else:
            print("  seeding (too old to alert): %s @ %s (posted %s)"
                  % (job["title"], job["company"], job["posted"]))

    # JPMC first: dream employer gets priority within the per-run cap.
    fresh_new.sort(key=lambda j: (j["company"] != "JPMorgan Chase",
                                  -(j["posted"].timestamp() if j["posted"] else 0)))

    sent = 0
    for job in fresh_new:
        if sent >= MAX_ALERTS_PER_RUN:
            post_discord({"username": "Job Watch", "content":
                          "…and **%d** more new matches this run (raising the "
                          "cap or check the repo log)." % (len(fresh_new) - sent)})
            break
        if "US" in job["regions"] and job.get("description") is None:
            if "_gh" in job:
                job["description"] = greenhouse_description(*job["_gh"])
            elif "_jpmc" in job:
                job["description"] = jpmc_description(job["_jpmc"])
        if "US" in job["regions"]:
            job["work_auth"] = work_auth_flag(job.get("description") or "")
        notify(job)
        sent += 1
        time.sleep(1.2)

    # Prune state entries not seen for 60 days (posting long gone).
    prune_before = (now - timedelta(days=60)).isoformat()
    for k in [k for k, v in state.items()
              if v.get("last_seen", "") < prune_before]:
        del state[k]

    if not DRY_RUN:
        save_state(state)
    print("new alerts sent: %d (state size: %d)" % (sent, len(state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
