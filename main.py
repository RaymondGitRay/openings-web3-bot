"""
Openings bot — fetches new jobs from free sources and posts them to Telegram channels.

Usage:
  python main.py            # normal run (posts to Telegram)
  python main.py --dry-run  # prints posts instead of sending, doesn't save state
  python main.py --check    # tests every company slug and prints which ones work
  python main.py --pin      # posts the welcome message with a DM button (then pins it)

Needs env var TELEGRAM_BOT_TOKEN (set as a GitHub Secret).
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "seen_jobs.json"
CONFIG_FILE = ROOT / "sources.yaml"
NOW = datetime.now(timezone.utc)

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0 (compatible; OpeningsJobsBot/1.0)"
TIMEOUT = 20


# ───────────────────────── helpers ─────────────────────────

def log(*a):
    print(*a, flush=True)


def parse_date(v):
    """Turn ISO strings / epoch (s or ms) into an aware datetime, or None."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def strip_html(s):
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", s).strip()


REMOTE_RE = re.compile(r"\b(remote|anywhere|worldwide|work from home|wfh|distributed|fully remote)\b", re.I)


def job(source, uid, title, company, location, url, posted=None, salary=None, remote=False,
        description=""):
    return {
        "id": f"{source}:{uid}",
        "dedupe": f"{norm(company)}|{norm(title)}",
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip() or ("Remote" if remote else ""),
        "url": url,
        "posted": parse_date(posted),
        "salary": salary,
        "description": strip_html(description)[:6000],
        "via": "",
        "remote": bool(remote) or bool(REMOTE_RE.search(location or "")),
    }


def fmt_money(lo, hi, cur="USD"):
    def k(x):
        return f"{int(x) // 1000}k" if x and x >= 1000 else (str(int(x)) if x else "")
    if not lo and not hi:
        return None
    sym = "$" if (cur or "USD").upper() == "USD" else f"{cur.upper()} "
    return f"{sym}{k(lo)}–{sym}{k(hi)}" if lo and hi else f"{sym}{k(lo or hi)}"


# ───────────────────────── fetchers ─────────────────────────

def fetch_greenhouse(slug, name):
    r = HTTP.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                 timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    out = []
    for j in r.json().get("jobs", []):
        out.append(job("gh", j["id"], j.get("title"), name,
                       (j.get("location") or {}).get("name"), j.get("absolute_url"),
                       j.get("first_published") or j.get("updated_at"),
                       description=j.get("content", "")))
    return out


def fetch_lever(slug, name):
    r = HTTP.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=TIMEOUT)
    if r.status_code != 200 or not isinstance(r.json(), list):
        return None
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        out.append(job("lv", j["id"], j.get("text"), name, cat.get("location"),
                       j.get("hostedUrl"), j.get("createdAt"),
                       fmt_money(sal.get("min"), sal.get("max"), sal.get("currency")),
                       remote=j.get("workplaceType") == "remote",
                       description=(j.get("descriptionPlain") or "") + "\n" + "\n".join(
                           f"{l.get('text', '')}: {strip_html(l.get('content', ''))}"
                           for l in (j.get("lists") or []))))
    return out


def fetch_ashby(slug, name):
    r = HTTP.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
                 timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append(job("ab", j.get("id") or j.get("jobUrl"), j.get("title"), name,
                       j.get("location"), j.get("jobUrl"), j.get("publishedAt"),
                       comp, remote=bool(j.get("isRemote")),
                       description=j.get("descriptionPlain") or j.get("descriptionHtml") or ""))
    return out


def fetch_lever_eu(slug, name):
    r = HTTP.get(f"https://api.eu.lever.co/v0/postings/{slug}?mode=json", timeout=TIMEOUT)
    if r.status_code != 200 or not isinstance(r.json(), list) or not r.json():
        return None
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        out.append(job("lv", j["id"], j.get("text"), name, cat.get("location"),
                       j.get("hostedUrl"), j.get("createdAt"),
                       remote=j.get("workplaceType") == "remote",
                       description=j.get("descriptionPlain") or ""))
    return out


def fetch_workable(slug, name):
    r = HTTP.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
                 timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    data = r.json()
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        out.append(job("wk", j.get("shortcode") or j.get("url"), j.get("title"), name, loc,
                       j.get("url") or j.get("application_url"),
                       j.get("published_on") or j.get("created_at"),
                       remote=bool(j.get("telecommuting")), description=j.get("description", "")))
    return out


def fetch_smartrecruiters(slug, name):
    r = HTTP.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
                 timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    content = r.json().get("content", [])
    if not content:  # unknown companies also return 200 with no postings
        return None
    out = []
    for j in content:
        l = j.get("location") or {}
        loc = ", ".join(x for x in [l.get("city"), l.get("country", "").upper()] if x)
        out.append(job("sr", j.get("id"), j.get("name"), name, loc,
                       f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                       j.get("releasedDate"), remote=bool(l.get("remote"))))
    return out


def fetch_recruitee(slug, name):
    try:
        r = HTTP.get(f"https://{slug}.recruitee.com/api/offers/", timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None
    if r.status_code != 200 or "offers" not in r.text[:200]:
        return None
    out = []
    for j in r.json().get("offers", []):
        out.append(job("rc", j.get("id"), j.get("title"), name,
                       j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x),
                       j.get("careers_url"), j.get("published_at"), remote=bool(j.get("remote")),
                       description=(j.get("description") or "") + " " + (j.get("requirements") or "")))
    return out


def fetch_bamboohr(slug, name):
    try:
        r = HTTP.get(f"https://{slug}.bamboohr.com/careers/list", timeout=TIMEOUT,
                     headers={"Accept": "application/json"})
    except requests.exceptions.ConnectionError:
        return None
    if r.status_code != 200 or "result" not in r.text[:300]:
        return None
    out = []
    for j in r.json().get("result", []):
        l = j.get("location") or {}
        loc = ", ".join(x for x in [l.get("city"), l.get("state")] if x)
        out.append(job("bh", j.get("id"), j.get("jobOpeningName"), name, loc,
                       f"https://{slug}.bamboohr.com/careers/{j.get('id')}", None,
                       remote=bool(j.get("isRemote"))))
    return out


def fetch_personio(slug, name):
    import xml.etree.ElementTree as ET
    for tld in ("de", "com"):
        try:
            r = HTTP.get(f"https://{slug}.jobs.personio.{tld}/xml?language=en", timeout=TIMEOUT)
        except requests.exceptions.ConnectionError:
            continue
        if r.status_code == 200 and "<position" in r.text:
            break
    else:
        return None
    out = []
    for pos in ET.fromstring(r.content).iter("position"):
        g = lambda t: (pos.findtext(t) or "").strip()
        desc = " ".join((d.findtext("value") or "") for d in pos.iter("jobDescription"))
        out.append(job("ps", g("id"), g("name"), name, g("office"),
                       f"https://{slug}.jobs.personio.{tld}/job/{g('id')}", g("createdAt"),
                       description=desc))
    return out


def fetch_breezy(slug, name):
    try:
        r = HTTP.get(f"https://{slug}.breezy.hr/json", timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None
    if r.status_code != 200 or not r.text.strip().startswith("["):
        return None
    out = []
    for j in r.json():
        l = j.get("location") or {}
        out.append(job("bz", j.get("id"), j.get("name"), name, l.get("name"), j.get("url"),
                       j.get("published_date"), remote=bool(l.get("is_remote"))))
    return out


def fetch_teamtailor(slug, name):
    try:
        r = HTTP.get(f"https://{slug}.teamtailor.com/jobs.rss", timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None
    if r.status_code != 200 or "<rss" not in r.text[:500]:
        return None
    feed = feedparser.parse(r.text)
    out = []
    for e in feed.entries:
        posted = time.mktime(e.published_parsed) if getattr(e, "published_parsed", None) else None
        loc = e.get("tt_location") or e.get("location") or ""
        out.append(job("tt", e.get("id") or e.get("link"), e.get("title"), name, loc,
                       e.get("link"), posted, description=e.get("summary", "")))
    return out


def fetch_pinpoint(slug, name):
    try:
        r = HTTP.get(f"https://{slug}.pinpointhq.com/postings.json", timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        return None
    if r.status_code != 200 or not r.text.strip().startswith("{"):
        return None
    data = r.json()
    items = data.get("data") or data.get("postings") or []
    out = []
    for j in items:
        a = j.get("attributes") or j
        l = a.get("location") or {}
        loc = l.get("name") if isinstance(l, dict) else str(l or "")
        out.append(job("pp", j.get("id"), a.get("title"), name, loc,
                       a.get("url") or f"https://{slug}.pinpointhq.com/en/postings/{j.get('id')}",
                       a.get("published_at") or a.get("created_at"),
                       description=a.get("description", "")))
    return out


ATS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
       "workable": fetch_workable, "lever_eu": fetch_lever_eu,
       "smartrecruiters": fetch_smartrecruiters, "recruitee": fetch_recruitee,
       "bamboohr": fetch_bamboohr, "personio": fetch_personio, "breezy": fetch_breezy,
       "teamtailor": fetch_teamtailor, "pinpoint": fetch_pinpoint}

# Patterns that reveal which job board a company uses, from any link or careers page.
BOARD_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([a-z0-9_-]+)"),
    ("lever_eu", r"jobs\.eu\.lever\.co/([a-z0-9_.-]+)"),
    ("lever", r"jobs\.lever\.co/([a-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)"),
    ("workable", r"apply\.workable\.com/([a-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9_-]+)"),
    ("recruitee", r"//([a-z0-9-]+)\.recruitee\.com"),
    ("bamboohr", r"//([a-z0-9-]+)\.bamboohr\.com"),
    ("personio", r"//([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("breezy", r"//([a-z0-9-]+)\.breezy\.hr"),
    ("teamtailor", r"//([a-z0-9-]+)\.teamtailor\.com"),
    ("pinpoint", r"//([a-z0-9-]+)\.pinpointhq\.com"),
]
NOT_SLUGS = {"embed", "api", "www", "jobs", "app", "careers", "job", "v1", "v0", "static",
             "assets", "cdn", "help", "support", "en", "de", "boards", "job-boards", "posting-api"}


def find_boards(text):
    """Return {(ats, slug)} for every job-board link found in a piece of text/HTML."""
    found = set()
    for ats, pat in BOARD_PATTERNS:
        for m in re.finditer(pat, text or "", re.I):
            slug = requests.utils.unquote(m.group(1)).lower().strip(". ")
            if (slug and slug not in NOT_SLUGS and len(slug) > 1 and "." not in slug
                    and not any(x in slug for x in ("cdn", "asset", "static", "image", "img"))):
                found.add((ats, slug))
    return found


_company_cache = {}


def fetch_company(slug, name, state):
    """Fetch a company's jobs once per run, even if several channels list it."""
    if slug not in _company_cache:
        _company_cache[slug] = _fetch_company(slug, name, state)
    return [dict(j, company=name) for j in _company_cache[slug]]


def _fetch_company(slug, name, state):
    """Use the cached job-board type, or try every supported board and remember which works."""
    cache = state.setdefault("platforms", {})
    known = cache.get(slug)
    if known and known.get("ats") == "none":
        # re-check dead slugs once a week
        if NOW - parse_date(known["checked"]) < timedelta(days=7):
            return []
        known = None
    order = [known["ats"]] if known else list(ATS)
    for ats in order:
        try:
            jobs = ATS[ats](slug, name)
        except Exception:
            jobs = None
        if jobs is not None:
            cache[slug] = {"ats": ats, "checked": NOW.isoformat()}
            for j in jobs:
                j["_slug"] = slug
            return jobs
    if known:  # cached platform stopped working → re-detect next run
        cache.pop(slug, None)
        return []
    cache[slug] = {"ats": "none", "checked": NOW.isoformat()}
    log(f"  ✗ '{slug}' not found on any supported job board — fix or remove it in sources.yaml")
    return []


def _walk_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk_jsonld(x)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield obj
        for k in ("@graph", "itemListElement", "item", "mainEntity"):
            if k in obj:
                yield from _walk_jsonld(obj[k])


def scan_careers_page(name, url):
    """Read a company careers page. Returns (job boards linked from it, jobs embedded in it)."""
    try:
        r = HTTP.get(url, timeout=TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml"})
    except Exception:
        return set(), []
    if not r.ok:
        return set(), []
    html_text = r.text
    boards = find_boards(html_text)
    jobs = []
    # 1) schema.org JobPosting data (used by many careers pages, Gem, Rippling, etc.)
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for jp in _walk_jsonld(data):
            org = jp.get("hiringOrganization") or {}
            locs = jp.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            loc = ", ".join(filter(None, [
                ((l.get("address") or {}).get("addressLocality") if isinstance(l.get("address"), dict) else "")
                for l in locs if isinstance(l, dict)]))
            remote = "TELECOMMUTE" in str(jp.get("jobLocationType", "")).upper()
            jobs.append(job("ld", jp.get("identifier") if isinstance(jp.get("identifier"), str)
                            else (jp.get("url") or jp.get("title")),
                            jp.get("title"), (org.get("name") if isinstance(org, dict) else "") or name,
                            loc, jp.get("url") or url, jp.get("datePosted"), remote=remote,
                            description=jp.get("description", "")))
    # 2) Comeet careers pages embed their positions as JSON
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", html_text, re.S)
    if m:
        try:
            for j in json.loads(m.group(1)):
                jobs.append(job("cm", j.get("uid") or j.get("position_uid"), j.get("name"), name,
                                (j.get("location") or {}).get("name", ""),
                                j.get("url_comeet_hosted_page") or j.get("url_active_page") or url,
                                j.get("time_updated"),
                                remote=bool((j.get("location") or {}).get("is_remote"))))
        except Exception:
            pass
    return boards, jobs


BRAVE_QUERIES = [
    "site:jobs.ashbyhq.com crypto", "site:jobs.ashbyhq.com web3", "site:jobs.ashbyhq.com blockchain",
    "site:jobs.lever.co crypto", "site:jobs.lever.co web3", "site:jobs.lever.co blockchain",
    "site:job-boards.greenhouse.io crypto", "site:boards.greenhouse.io web3",
    "site:job-boards.greenhouse.io stablecoin", "site:apply.workable.com crypto",
    "site:apply.workable.com web3", "site:jobs.smartrecruiters.com crypto",
    "site:teamtailor.com web3", "site:bamboohr.com crypto careers", "site:jobs.personio.de crypto",
    "site:breezy.hr crypto", "site:recruitee.com web3", "site:jobs.ashbyhq.com defi",
    "site:jobs.lever.co defi", "site:job-boards.greenhouse.io digital assets",
]


def brave_discover(state, queries=None, channel="web3", per_run=1, monthly_budget=900):
    """Find new companies' job boards with Brave Search (free monthly credits). Needs BRAVE_API_KEY."""
    key = os.environ.get("BRAVE_API_KEY")
    queries = queries or BRAVE_QUERIES
    if not key:
        return set()
    month = NOW.strftime("%Y-%m")
    b = state.setdefault("brave", {})
    if b.get("month") != month:
        b.update(month=month, used=0)
    found = set()
    for _ in range(per_run):
        if b["used"] >= monthly_budget:
            break
        nxt = b.setdefault("next_by_channel", {})
        i = nxt.get(channel, 0)
        q = queries[i % len(queries)]
        offset = (i // len(queries)) % 5  # page deeper on each pass through the list
        nxt[channel] = i + 1
        b["used"] += 1
        try:
            r = HTTP.get("https://api.search.brave.com/res/v1/web/search", timeout=30,
                         headers={"X-Subscription-Token": key, "Accept": "application/json"},
                         params={"q": q, "count": 20, "offset": offset})
            if not r.ok:
                log(f"  ! Brave {r.status_code}: {r.text[:150]}")
                continue
            for res in (r.json().get("web") or {}).get("results", []):
                found |= find_boards(res.get("url", "") + " " + res.get("description", ""))
        except Exception as e:
            log(f"  ! Brave error: {e}")
    return found


def remember_boards(state, boards, known_slugs, source, names=None, channel="web3"):
    """Add newly found job boards to the permanent discovered list (owned by one channel)."""
    disc = state.setdefault("discovered", {})
    added = 0
    for ats, slug in boards:
        key = f"{ats}:{slug}"
        if key in disc or slug in known_slugs:
            continue
        disc[key] = {"name": (names or {}).get((ats, slug)) or slug.replace("-", " ").title(),
                     "found": NOW.isoformat()[:10], "via": source, "ch": channel}
        added += 1
    return added


def fetch_discovered(state, keywords, channel="web3"):
    """Fetch this channel's discovered boards; keep only jobs that mention its keywords."""
    from concurrent.futures import ThreadPoolExecutor
    all_disc = state.setdefault("discovered", {})
    disc = {k: v for k, v in all_disc.items() if v.get("ch", "web3") == channel}

    def one(item):
        key, meta = item
        ats, slug = key.split(":", 1)
        if key not in _company_cache:
            try:
                _company_cache[key] = ATS[ats](slug, meta["name"])
            except Exception:
                _company_cache[key] = None
        got = _company_cache[key]
        return key, (None if got is None else [dict(j) for j in got])

    jobs, dead, live = [], [], 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for key, got in pool.map(one, list(disc.items())):
            if got is None:
                all_disc[key]["fails"] = all_disc[key].get("fails", 0) + 1
                if all_disc[key]["fails"] >= 3:
                    dead.append(key)
                continue
            all_disc[key]["fails"] = 0
            live += 1
            for j in got:
                j["_slug"] = key
            jobs += [j for j in got if matches(j["title"] + " " + j["description"], keywords)]
    for key in dead:
        all_disc.pop(key, None)
    log(f"  discovered boards: {len(jobs)} matching jobs from {live} of {len(disc)} boards")
    return jobs


_remote_cache = {}


def fetch_remoteok():
    if "remoteok" not in _remote_cache:
        r = HTTP.get("https://remoteok.com/api", timeout=TIMEOUT)
        data = r.json() if r.ok else []
        out = []
        for j in data[1:]:  # first item is a legal notice
            out.append(job("rok", j.get("id"), j.get("position"), j.get("company"),
                           j.get("location") or "Remote", j.get("url"), j.get("date"),
                           fmt_money(j.get("salary_min"), j.get("salary_max")), remote=True,
                           description=j.get("description", ""))
                       | {"_text": " ".join([j.get("position", ""), " ".join(j.get("tags") or [])]),
                          "via": "RemoteOK"})
        _remote_cache["remoteok"] = out
    return _remote_cache["remoteok"]


def fetch_web3career():
    """web3.career free API. Terms: link to apply_url, no extra tracking params."""
    token = os.environ.get("WEB3CAREER_TOKEN")
    if not token:
        return []
    r = HTTP.get("https://web3.career/api/v1", timeout=40,
                 params={"token": token, "limit": 100, "show_description": "true"})
    if not r.ok:
        log(f"  ! web3.career {r.status_code}: {r.text[:150]}")
        return []
    data = r.json()
    items = data if data and isinstance(data[0], dict) else next(
        (x for x in data if isinstance(x, list)), [])
    out = []
    for j in items:
        x = job("w3c", j.get("id") or j.get("url"), j.get("title"), j.get("company"),
                j.get("location"), j.get("apply_url") or j.get("url"), j.get("postedAt") or j.get("date"),
                j.get("salary") or None, remote=bool(j.get("remote")),
                description=j.get("description", ""))
        x["via"] = "web3.career"
        out.append(x)
    return out


def fetch_cryptojobslist():
    """CryptoJobsList public API (free with link-back to canonicalURL)."""
    key = os.environ.get("CJL_API_KEY")
    if not key:
        return []
    base = os.environ.get("CJL_API_BASE", "https://cryptojobslist.com/api").rstrip("/")
    r = HTTP.get(f"{base}/public/jobs", headers={"x-api-key": key}, timeout=40,
                 params={"limit": 100})
    if not r.ok:
        log(f"  ! CryptoJobsList {r.status_code}: {r.text[:150]}")
        return []
    data = r.json()
    items = data if isinstance(data, list) else (data.get("jobs") or data.get("data")
                                                 or data.get("results") or [])
    out = []
    for j in items:
        comp = j.get("companyName") or j.get("company")
        if isinstance(comp, dict):
            comp = comp.get("name")
        x = job("cjl", j.get("id") or j.get("_id") or j.get("canonicalURL"),
                j.get("jobTitle") or j.get("title"), comp,
                j.get("jobLocation") or j.get("location") or ("Remote" if j.get("remote") else ""),
                j.get("canonicalURL") or j.get("url"),
                j.get("publishedAt") or j.get("createdAt"), j.get("salaryString") or None,
                remote=bool(j.get("remote")), description=j.get("description", ""))
        x["via"] = "CryptoJobsList"
        out.append(x)
    return out


GOOGLE_QUERIES = [
    "web3 jobs", "blockchain developer", "crypto business development", "crypto compliance",
    "web3 marketing", "defi engineer", "crypto jobs singapore", "web3 jobs remote",
    "stablecoin jobs", "digital assets jobs", "crypto exchange jobs", "smart contract engineer",
]


TRUSTED_APPLY = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
                 "recruitee.com", "bamboohr.com", "personio.", "breezy.hr", "teamtailor.com", "pinpointhq.com",
                 "myworkdayjobs.com", "linkedin.com", "wellfound.com", "web3.career", "cryptojobslist.com")


def best_apply_link(j):
    """Prefer the company's own site, its job board or LinkedIn over job-reposting websites."""
    opts = j.get("apply_options") or []
    company = re.sub(r"[^a-z0-9]", "", (j.get("company_name") or "").lower())
    for o in opts:
        link = o.get("link", "")
        host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0].lower()
        if any(t in link for t in TRUSTED_APPLY) or (company and company[:6] in host.replace("-", "")):
            return re.sub(r"[?&]utm_[^&]+", "", link).rstrip("?&")
    return j.get("share_link") or (opts[0]["link"] if opts else None)


def fetch_google_jobs(state, query=None, monthly_budget=240):
    """Google Jobs via SerpApi (free plan: 250 searches/month). One search per run across all channels."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return []
    month = NOW.strftime("%Y-%m")
    g = state.setdefault("google", {})
    if g.get("month") != month:
        g.update(month=month, used=0)
    if g["used"] >= monthly_budget:
        log("  Google Jobs: monthly budget used up")
        return []
    q = query or GOOGLE_QUERIES[g.get("next", 0) % len(GOOGLE_QUERIES)]
    g["used"] += 1
    r = HTTP.get("https://serpapi.com/search.json", timeout=60,
                 params={"engine": "google_jobs", "q": q, "hl": "en", "api_key": key})
    if not r.ok:
        log(f"  ! Google Jobs {r.status_code}: {r.text[:150]}")
        return []
    out = []
    for j in r.json().get("jobs_results", []):
        posted = (j.get("detected_extensions") or {}).get("posted_at", "")
        m = re.match(r"(\d+)\s+(hour|day)", posted)
        if m and m.group(2) == "day" and int(m.group(1)) > 3:
            continue
        link = best_apply_link(j)
        via = (j.get("via") or "").replace("via ", "").strip()
        x = job("gj", j.get("job_id") or link, j.get("title"), j.get("company_name"),
                j.get("location"), link, None,
                (j.get("detected_extensions") or {}).get("salary"),
                description=j.get("description", ""))
        x["via"] = f"Google Jobs{' / ' + via if via else ''}"
        x["_text"] = f"{j.get('title', '')} {j.get('company_name', '')} {j.get('description', '')[:1500]}"
        out.append(x)
    log(f"  Google Jobs '{q}': {len(out)} recent results ({g['used']}/{monthly_budget} this month)")
    return out


def fetch_rss(url):
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries:
        posted = None
        if getattr(e, "published_parsed", None):
            posted = time.mktime(e.published_parsed)
        out.append(job("rss", e.get("id") or e.get("link"), e.get("title"),
                       e.get("author") or feed.feed.get("title", ""), "", e.get("link"), posted,
                       description=e.get("summary", ""))
                   | {"_text": e.get("title", "") + " " + e.get("summary", "")})
    return out


def matches(text, keywords):
    t = (text or "").lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", t) for k in keywords)


# ───────────────────────── telegram ─────────────────────────

# Job function → (hashtag, who-it-suits phrase). First match wins, so order matters.
FUNCTIONS = [
    ("Intern", ["intern", "internship", "graduate", "trainee"], "students and fresh grads"),
    ("People", ["hrbp", "hr business partner", "human resources", "recruit", "talent acquisition", "people partner", "people operations", "hr "], "HR and talent professionals"),
    ("Security", ["security", "appsec", "infosec", "auditor", "penetration"], "security engineers and auditors"),
    ("Data", ["data scientist", "data engineer", "data analyst", "analytics", "machine learning", "ml engineer", "ai engineer", "research scientist"], "data / ML people"),
    ("Engineering", ["engineer", "developer", "devops", "sre", "architect", "solidity", "smart contract", "backend", "frontend", "full stack", "fullstack", "blockchain", "protocol", "infrastructure", "qa", "programmer"], "engineers"),
    ("Product", ["product manager", "product owner", "product lead", "head of product", "program manager", "project manager", "technical program"], "product and program managers"),
    ("Design", ["designer", "design", "ux", "ui ", "creative"], "designers"),
    ("Compliance", ["compliance", "aml", "kyc", "risk", "legal", "counsel", "regulatory", "licensing", "audit", "fraud"], "compliance, risk and legal professionals"),
    ("Finance", ["finance", "accountant", "accounting", "treasury", "controller", "tax", "fp&a", "trader", "trading", "quant", "investment", "analyst"], "finance, trading and investment people"),
    ("BD", ["business development", "bd ", "partnership", "sales", "account executive", "account manager", "institutional", "relationship manager"], "BD and sales people"),
    ("Marketing", ["marketing", "growth", "community", "content", "social media", "brand", "communications", "pr ", "seo", "ecosystem", "devrel", "developer relations", "events", "kol"], "marketers and community builders"),
    ("CustomerSuccess", ["customer success", "customer support", "support", "client service", "onboarding", "customer experience", "implementation"], "customer success and support people"),
    ("Operations", ["operations", "ops", "chief of staff", "administrator", "office", "executive assistant", "strategy"], "operators and generalists"),
    ("People", ["recruit", "talent", "people", "hr ", "human resources"], "recruiters and HR people"),
]
SENIORITY = [
    (["chief", "vp", "vice president", "head of", "director", "general manager"], "Senior leaders"),
    (["principal", "staff", "lead", "manager"], "Experienced"),
    (["senior", "sr.", "sr "], "Senior"),
    (["junior", "jr.", "associate", "entry"], "Early-career"),
    (["intern", "graduate", "trainee"], ""),
]
PLACES = {
    "Remote": ["remote", "anywhere", "worldwide", "global"], "Singapore": ["singapore"],
    "HongKong": ["hong kong"], "UAE": ["dubai", "uae", "abu dhabi"], "Malaysia": ["malaysia", "kuala lumpur"],
    "Philippines": ["philippines", "manila"], "Indonesia": ["indonesia", "jakarta"], "Vietnam": ["vietnam", "ho chi minh", "hanoi"],
    "Thailand": ["thailand", "bangkok"], "Australia": ["australia", "sydney", "melbourne"], "London": ["london"],
    "NewYork": ["new york", "nyc"], "SanFrancisco": ["san francisco"], "Tokyo": ["tokyo", "japan"],
    "Seoul": ["seoul", "korea"], "India": ["india", "bangalore", "bengaluru", "mumbai", "delhi"],
    "Europe": ["europe", "emea", "berlin", "paris", "lisbon", "amsterdam", "zurich", "dublin"],
    "USA": ["united states", "usa", " us", "us-", "us ", "canada", "toronto"], "APAC": ["apac", "asia"],
}


# Short or ambiguous keywords must match as whole words ("intern" must not match "internal")
WHOLE_WORDS = {"intern", "internship", "ops", "qa", "ux", "ui", "sre", "hr", "pr", "bd", "ai", "ml", "tax",
               "risk", "kol", "people", "office", "content", "brand", "card", "events", "growth", "lead",
               "staff", "chief", "vp", "head of", "associate", "entry", "junior", "senior", "graduate",
               "trainee", "design", "data", "aml", "kyc", "audit", "legal", "support", "strategy"}


def has_any(text, words):
    t = f" {text.lower()} "
    for w in words:
        k = w.strip()
        tail = r"(?![a-z])" if k in WHOLE_WORDS else ""
        if re.search(r"(?<![a-z])" + re.escape(k) + tail, t):
            return True
    return False


def classify(j):
    t = j["title"].lower() + " "
    primary = re.split(r",| - | – | \| |\(", t)[0] + " "  # role part, before the department
    func = (next((f for f in FUNCTIONS if has_any(primary, f[1])), None)
            or next((f for f in FUNCTIONS if has_any(t, f[1])), None))
    level = next((lbl for words, lbl in SENIORITY if has_any(t, words)), "")
    m = re.search(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*\+?\s*years", j["description"], re.I)
    years = int(m.group(1)) if m and 0 < int(m.group(1)) < 20 else None
    loc = j["location"].lower()
    places = [p for p, words in PLACES.items() if has_any(loc, words)][:2]
    return func, level, years, places


LEADER_LABEL = {"Engineering": "engineering", "Data": "data / AI", "Security": "security",
                "Product": "product", "Design": "design", "Compliance": "compliance & risk",
                "Finance": "finance", "BD": "BD & sales", "Marketing": "marketing",
                "CustomerSuccess": "customer success", "Operations": "operations",
                "People": "people & talent"}


def who_rule_based(j):
    func, level, years, _ = classify(j)
    if func and func[0] == "Intern":
        return "Students and fresh grads keen to start a career in Web3."
    if level == "Senior leaders":
        who = f"Senior {LEADER_LABEL.get(func[0], '') + ' ' if func else ''}leaders"
    else:
        who = func[2] if func else "professionals"
        who = f"{level} {who}" if level else who[0].upper() + who[1:]
    exp = f" with {years}+ years of experience" if years else ""
    return f"{who}{exp} who want to build in Web3."


_gemini = {"models": None, "failures": 0}


def gemini_models(key):
    """Ask Google which models this key can use. Fast 'flash-lite' first, then 'flash'; stable before preview."""
    if _gemini["models"] is not None:
        return _gemini["models"]
    forced = os.environ.get("GEMINI_MODEL")
    if forced:
        _gemini["models"] = [forced]
        return _gemini["models"]
    try:
        r = HTTP.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                     headers={"x-goog-api-key": key}, timeout=20)
        models = r.json().get("models", []) if r.ok else []
    except Exception as e:
        log(f"  ! Gemini model list error: {e}")
        models = []
    bad = ("image", "tts", "live", "audio", "embedding", "exp", "thinking", "robotics", "computer")

    def ver(name):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        return float(m.group(1)) if m else 0

    names = [m["name"].split("/")[-1] for m in models
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    flash = [n for n in names if "flash" in n and not any(b in n for b in bad)]
    ranked = sorted(flash, key=lambda n: (("preview" not in n), ("lite" in n), ver(n), -len(n)),
                    reverse=True)
    _gemini["models"] = ranked[:4]
    log(f"  Gemini models to try: {', '.join(_gemini['models']) or 'none available'}")
    return _gemini["models"]


def gemini_call(key, model, prompt, thinking=True):
    cfg = {"temperature": 0.3, "maxOutputTokens": 1024}
    if thinking:
        cfg["thinkingConfig"] = {"thinkingLevel": "low"}
    return HTTP.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key}, timeout=25,
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg})


def who_ai(j):
    """One-sentence 'who should apply' via Google Gemini free tier. Returns None on any issue."""
    key = os.environ.get("GEMINI_API_KEY")
    if (not key or not j["description"] or _gemini["failures"] >= 3
            or _gemini.get("calls", 0) >= _gemini.get("max_calls", 80)):
        return None
    _gemini["calls"] = _gemini.get("calls", 0) + 1
    prompt = (
        "You write one line for a job-board post. Based on the job below, say WHO SHOULD APPLY: "
        "the ideal candidate's background, years of experience, and 2-3 key skills. "
        "Max 30 words, plain text, no emojis, no intro like 'Ideal candidate:'. "
        "Start with the type of person (e.g. 'Backend engineers with 5+ years...').\n\n"
        f"Title: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
        f"Description:\n{j['description'][:4000]}"
    )
    for model in list(gemini_models(key)):
        try:
            r = gemini_call(key, model, prompt)
            if r.status_code == 400 and "thinking" in r.text.lower():
                r = gemini_call(key, model, prompt, thinking=False)
            if not r.ok:
                log(f"  ! Gemini {model} {r.status_code}: {r.text[:200]}")
                raise RuntimeError("bad status")
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            text = re.sub(r"\s+", " ", text).strip(' "')
            if 10 < len(text) < 300:
                _gemini["models"] = [model] + [m for m in _gemini["models"] if m != model]
                return text
            raise RuntimeError("empty answer")
        except Exception as e:
            log(f"  ! Gemini {model} failed: {str(e)[:120]}")
            _gemini["failures"] += 1
            _gemini["models"] = [m for m in _gemini["models"] if m != model] + [model]
            if _gemini["failures"] >= 3:
                log("  ! Gemini disabled for this run, using built-in lines")
                return None
    return None


_who_cache = {}


def format_post(j, ch):
    e = html.escape
    func, _, _, places = classify(j)
    if j["dedupe"] not in _who_cache:  # same job in several channels → one AI call
        _who_cache[j["dedupe"]] = who_ai(j) or who_rule_based(j)
    who = _who_cache[j["dedupe"]]
    lines = [
        f"🏢 <b>{e(j['company'])}</b>",
        f"💼 {e(j['title'])}",
    ]
    if j["location"]:
        lines.append(f"📍 {e(j['location'])}")
    if j.get("salary"):
        lines.append(f"💰 {e(str(j['salary']))}")
    lines += [
        "",
        f"🎯 <b>Who should apply:</b> {e(who)}",
        "",
        f'🔗 <a href="{e(j["url"], quote=True)}">Apply here</a>'
        + (f" <i>(via {e(j['via'])})</i>" if j.get("via") else ""),
        "",
    ]
    tags = [ch.get("hashtag", "")]
    if func:
        tags.append("#" + func[0])
    tags += ["#" + p for p in places]
    if j["company"]:
        tags.append("#" + re.sub(r"[^A-Za-z0-9]", "", j["company"]))
    lines.append(" ".join(t for t in tags if t))
    if ch.get("footer"):
        lines.append(f"<i>{e(ch['footer'])}</i>")
    return "\n".join(lines)


# ───────────────────────── digest format ─────────────────────────

# (label, flag, hashtag, patterns) — first match wins; order roughly by importance to the audience
COUNTRIES = [
    ("Singapore", "🇸🇬", "Singapore", ["singapore"]),
    ("Hong Kong", "🇭🇰", "HongKong", ["hong kong", "hongkong"]),
    ("UAE", "🇦🇪", "UAE", ["dubai", "abu dhabi", "uae", "united arab emirates"]),
    ("India", "🇮🇳", "India", ["india", "bangalore", "bengaluru", "mumbai", "delhi", "gurgaon", "gurugram",
                             "hyderabad", "pune", "chennai", "noida"]),
    ("Japan", "🇯🇵", "Japan", ["japan", "tokyo", "osaka"]),
    ("South Korea", "🇰🇷", "Korea", ["korea", "seoul"]),
    ("Taiwan", "🇹🇼", "Taiwan", ["taiwan", "taipei"]),
    ("China", "🇨🇳", "China", ["china", "shanghai", "beijing", "shenzhen", "hangzhou"]),
    ("Malaysia", "🇲🇾", "Malaysia", ["malaysia", "kuala lumpur"]),
    ("Indonesia", "🇮🇩", "Indonesia", ["indonesia", "jakarta", "bali"]),
    ("Philippines", "🇵🇭", "Philippines", ["philippines", "manila", "makati", "taguig"]),
    ("Vietnam", "🇻🇳", "Vietnam", ["vietnam", "ho chi minh", "hanoi"]),
    ("Thailand", "🇹🇭", "Thailand", ["thailand", "bangkok"]),
    ("Australia", "🇦🇺", "Australia", ["australia", "sydney", "melbourne", "brisbane", "perth"]),
    ("New Zealand", "🇳🇿", "NewZealand", ["new zealand", "auckland", "wellington"]),
    ("USA", "🇺🇸", "USA", ["united states", "usa", "u.s.", "new york", "nyc", "san francisco", "los angeles",
                         "seattle", "austin", "chicago", "boston", "miami", "denver", "palo alto",
                         "mountain view", "menlo park", "washington", ", ca", ", ny", ", tx", ", wa", ", ma",
                         ", fl", ", co", ", il", ", nv", ", ga", ", nc", ", va", ", ut", ", az", " us",
                         "us-", "-us", "california", "texas", "florida", "colorado", "illinois",
                         "massachusetts", "virginia", "utah", "arizona", "oregon", "pennsylvania",
                         "new jersey", "indiana", "indianapolis", "ohio", "michigan", "minnesota",
                         "tennessee", "north carolina", "nevada"] + [", " + c for c in (
                         "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne "
                         "nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc").split()]),
    ("Canada", "🇨🇦", "Canada", ["canada", "toronto", "vancouver", "montreal"]),
    ("UK", "🇬🇧", "UK", ["united kingdom", "london", " uk", "uk-", "england", "manchester", "edinburgh"]),
    ("Ireland", "🇮🇪", "Ireland", ["ireland", "dublin"]),
    ("Germany", "🇩🇪", "Germany", ["germany", "berlin", "munich", "hamburg", "frankfurt"]),
    ("France", "🇫🇷", "France", ["france", "paris"]),
    ("Switzerland", "🇨🇭", "Switzerland", ["switzerland", "zurich", "zürich", "zug", "geneva", "lausanne"]),
    ("Netherlands", "🇳🇱", "Netherlands", ["netherlands", "amsterdam", "rotterdam"]),
    ("Portugal", "🇵🇹", "Portugal", ["portugal", "lisbon", "porto"]),
    ("Spain", "🇪🇸", "Spain", ["spain", "madrid", "barcelona"]),
    ("Poland", "🇵🇱", "Poland", ["poland", "warsaw", "krakow"]),
    ("Austria", "🇦🇹", "Austria", ["austria", "vienna"]),
    ("Italy", "🇮🇹", "Italy", ["italy", "milan", "rome"]),
    ("Israel", "🇮🇱", "Israel", ["israel", "tel aviv"]),
    ("Turkey", "🇹🇷", "Turkey", ["turkey", "türkiye", "istanbul"]),
    ("Brazil", "🇧🇷", "Brazil", ["brazil", "são paulo", "sao paulo"]),
    ("Mexico", "🇲🇽", "Mexico", ["mexico"]),
    ("Argentina", "🇦🇷", "Argentina", ["argentina", "buenos aires"]),
    ("Nigeria", "🇳🇬", "Nigeria", ["nigeria", "lagos"]),
    ("South Africa", "🇿🇦", "SouthAfrica", ["south africa", "cape town", "johannesburg"]),
    ("Europe", "🇪🇺", "Europe", ["europe", "emea", "eu "]),
    ("APAC", "🌏", "APAC", ["apac", "asia"]),
]


def group_of(j):
    """Which country section a job goes in. Fully remote jobs get their own section."""
    if j.get("remote"):
        return ("Remote", "🌐", "Remote")
    loc = f" {(j.get('location') or '').lower()} "
    for label, flag, tag, pats in COUNTRIES:
        if any(re.search(re.escape(p) + r"(?![a-z])", loc) for p in pats):
            return (label, flag, tag)
    return ("Other locations", "🌍", "")


def digest_messages(jobs, ch, max_chars=3600):
    """Build compact messages: jobs grouped by country, one line per job."""
    e = html.escape
    groups = {}
    for j in jobs:
        groups.setdefault(group_of(j), []).append(j)
    # Remote and Singapore first, then the biggest groups
    priority = {"Remote": 0, "Singapore": 1}
    ordered = sorted(groups.items(), key=lambda kv: (priority.get(kv[0][0], 2), -len(kv[1]), kv[0][0]))
    name = ch.get("title") or ch.get("hashtag", "#Jobs").lstrip("#").replace("Jobs", " jobs")
    header = f"🗂 <b>New {e(name)}</b> · {NOW.strftime('%d %b')} · {len(jobs)} role{'s' if len(jobs) != 1 else ''}"
    tags = [ch.get("hashtag", "")] + ["#" + g[2] for g, _ in ordered if g[2]]
    funcs = sorted({f[0] for f in (classify(j)[0] for j in jobs) if f})
    tag_line = " ".join(t for t in tags if t) + ("\n" + " ".join("#" + f for f in funcs) if funcs else "")
    footer = (f"\n{tag_line}" + (f"\n<i>{e(ch['footer'])}</i>" if ch.get("footer") else ""))

    blocks = []
    for (label, flag, _), js in ordered:
        lines = [f"\n{flag} <b>{e(label)}</b> ({len(js)})"]
        for j in sorted(js, key=lambda j: j["company"].lower()):
            extra = ""
            if label in ("Remote", "Other locations", "Europe", "APAC") and j.get("location"):
                loc = j["location"] if len(j["location"]) <= 28 else j["location"][:26] + "…"
                extra = f" · {e(loc)}"
            if j.get("via") == "RemoteOK":
                extra += " · via RemoteOK"
            lines.append(f'• {e(j["company"])} is hiring <a href="{e(j["url"], quote=True)}">'
                         f'{e(j["title"])}</a>{extra}')
        blocks.append(lines)

    # Split into Telegram-sized messages (limit counts visible text; keep a safe margin)
    visible = lambda t: len(re.sub(r"<[^>]+>", "", t))
    messages, cur = [], header
    for lines in blocks:
        for i, line in enumerate(lines):
            if visible(cur) + visible(line) + visible(footer) + 1 > max_chars:
                messages.append(cur + footer)
                cur = header + " (cont.)" + ("" if i == 0 else f"\n{lines[0].strip()} (cont.)")
            cur += "\n" + line
    messages.append(cur + footer)
    return messages


def send(token, chat_id, text):
    for attempt in range(3):
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=TIMEOUT, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if r.ok:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 30)
            log(f"  rate limited, waiting {wait}s")
            time.sleep(wait + 1)
            continue
        log(f"  ! Telegram error {r.status_code}: {r.text[:200]}")
        return False
    return False


# ───────────────────────── main ─────────────────────────

def collect(ch, state, key="web3", google_query=None):
    """Gather every job for one channel from all of its configured sources."""
    from concurrent.futures import ThreadPoolExecutor
    jobs, found = [], 0
    companies = list((ch.get("companies") or {}).items())
    known_slugs = {slug for slug, _ in companies}
    if companies:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for got in pool.map(lambda kv: fetch_company(kv[0], kv[1] or kv[0], state), companies):
                if got:
                    found += 1
                jobs += got
    kw = ch.get("keywords") or []
    check = set(ch.get("keyword_check_companies") or [])
    jobs = [j for j in jobs if j.get("_slug") not in check
            or matches(j["title"] + " " + j["description"], kw)]
    if companies:
        log(f"  company pages: {len(jobs)} jobs from {found} companies")

    # Careers pages: find which job board each company uses, plus jobs embedded in the page
    pages = list((ch.get("careers_pages") or {}).items())
    if pages:
        page_jobs, page_boards, names = [], set(), {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for (name, url), (boards, pj) in zip(pages, pool.map(lambda kv: scan_careers_page(*kv), pages)):
                page_boards |= boards
                for b in boards:
                    names.setdefault(b, name)
                page_jobs += pj
        added = remember_boards(state, page_boards, known_slugs, "careers page", names, key)
        jobs += page_jobs
        log(f"  careers pages: {len(page_jobs)} embedded jobs, {len(page_boards)} job boards found "
            f"({added} new)")

    def safe(name, fn):
        try:
            got = fn()
            if got:
                log(f"  {name}: {len(got)} jobs")
            return got
        except Exception as e:
            log(f"  ! {name} error: {e}")
            return []

    # Web3-only boards: no keyword filter needed
    if ch.get("web3career"):
        jobs += safe("web3.career", fetch_web3career)
    if ch.get("cryptojobslist"):
        jobs += safe("CryptoJobsList", fetch_cryptojobslist)

    # General sources: keep only jobs matching the channel keywords (all jobs if no keywords)
    extra = []
    if ch.get("remoteok"):
        extra += safe("RemoteOK", fetch_remoteok)
    if google_query:
        extra += safe("Google Jobs", lambda: fetch_google_jobs(state, google_query))
    for url in ch.get("rss") or []:
        extra += safe(f"RSS {url}", lambda u=url: fetch_rss(u))
    if extra:
        kept = [j for j in extra if not kw or matches(j.get("_text") or j["title"], kw)]
        log(f"  general sources: {len(kept)} of {len(extra)} match keywords")
        jobs += kept

    # Auto-discovery: any job link that points at a company job board becomes a permanent source
    if ch.get("discover"):
        link_boards, link_names = set(), {}
        for j in jobs:
            for b in find_boards(j["url"] or ""):
                link_boards.add(b)
                link_names.setdefault(b, j["company"])
        added = remember_boards(state, link_boards, known_slugs, "job link", link_names, key)
        brave = brave_discover(state, ch.get("brave_queries"), key) if ch.get("brave_queries") else set()
        added_b = remember_boards(state, brave, known_slugs, "brave search", None, key)
        log(f"  discovery: +{added} boards from job links, +{added_b} from search")
        if kw:
            jobs += fetch_discovered(state, kw, key)
    return jobs


def pool_jobs(ch, collected):
    """For channels like Remote and Tech: pick matching jobs found by other channels."""
    pool = ch.get("pool") or {}
    picked = []
    funcs = set(pool.get("functions") or [])
    title_kw = pool.get("title_keywords") or []
    for src in pool.get("from") or []:
        for j in collected.get(src, []):
            if pool.get("remote_only") and not j.get("remote"):
                continue
            if funcs:
                f = classify(j)[0]
                if not f or f[0] not in funcs:
                    continue
            if title_kw and not matches(j["title"], title_kw):
                continue
            picked.append(dict(j))
    if pool:
        log(f"  pooled from {', '.join(pool.get('from') or [])}: {len(picked)} matching jobs")
    return picked


def post_pinned(token, cfg):
    """Post the welcome message with a DM button to every channel that has one configured."""
    for key, ch in cfg["channels"].items():
        pin = ch.get("pinned") or {}
        path = ROOT / pin.get("file", "pinned_message.html")
        if not pin or not path.exists():
            continue
        payload = {
            "chat_id": ch["chat_id"], "text": path.read_text(), "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[{"text": pin.get("button", "💬 DM us"),
                                                   "url": pin["url"]}]]},
        }
        r = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=TIMEOUT)
        log(f"  pinned message for {key}: {r.status_code} {r.text[:200]}")
        if r.ok:
            mid = r.json()["result"]["message_id"]
            p = HTTP.post(f"https://api.telegram.org/bot{token}/pinChatMessage", timeout=TIMEOUT,
                          json={"chat_id": ch["chat_id"], "message_id": mid,
                                "disable_notification": True})
            log("  pinned it" if p.ok else
                f"  posted, but couldn't pin ({p.text[:120]}) — pin it yourself: long-press → Pin")


def main():
    dry = "--dry-run" in sys.argv
    check = "--check" in sys.argv
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    s = cfg.get("settings", {})
    max_posts = int(s.get("max_posts_per_run", 2))
    max_age = timedelta(days=int(s.get("max_job_age_days", 10)))
    first_posts = int(s.get("first_run_posts", 3))
    delay = float(s.get("delay_between_posts", 3.2))
    _gemini["max_calls"] = int(s.get("ai_max_calls_per_run", 80))

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    seen = state.setdefault("seen", {})
    # One-time upgrade: older entries had no channel prefix
    for k in [k for k in seen if "::" not in k]:
        v = seen.pop(k)
        seen[f"{v.get('ch', 'web3')}::{k}"] = v
    if check:
        state["platforms"] = {}  # force re-detection

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token and not (dry or check):
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    if "--pin" in sys.argv:
        post_pinned(token, cfg)
        return

    channels = {k: v for k, v in cfg["channels"].items() if v.get("enabled", True)}

    # Google Jobs: one search per run, rotating through every channel's queries
    pairs = [(k, q) for k, ch in channels.items() for q in (ch.get("google_queries") or [])]
    g = state.setdefault("google", {})
    pick = pairs[g.get("next", 0) % len(pairs)] if pairs else (None, None)
    g["next"] = g.get("next", 0) + 1

    # Phase 1: every channel collects from its own sources
    order = list(channels)
    collected, queues = {}, {}
    for key in order:
        ch = channels[key]
        log(f"\n=== {key} → {ch['chat_id']} ===")
        collected[key] = collect(ch, state, key, pick[1] if pick[0] == key else None)

    # Phase 2: add pooled jobs (Remote, Tech, AI), filter, and pick what's new for each channel
    for key in order:
        ch = channels[key]
        log(f"\n--- {key}: choosing new jobs ---")
        jobs = list(collected[key]) + pool_jobs(ch, collected)
        if ch.get("remote_only"):
            jobs = [j for j in jobs if j.get("remote")]
            log(f"  remote only: {len(jobs)} remote jobs")
        if check:
            continue
        sk = lambda k: f"{key}::{k}"
        first_run = not any(k.startswith(f"{key}::") for k in seen)

        fresh, batch_keys = [], set()
        for j in jobs:
            if not j["url"] or not j["title"]:
                continue
            ukey = "url:" + j["url"].split("?")[0].rstrip("/").lower()
            if (sk(j["id"]) in seen or sk(j["dedupe"]) in seen or sk(ukey) in seen
                    or j["dedupe"] in batch_keys or ukey in batch_keys):
                continue
            if j["posted"] and NOW - j["posted"] > max_age:
                continue
            batch_keys.update([j["dedupe"], ukey])
            j["ukey"] = ukey
            fresh.append(j)
        fresh.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        limit = int(ch.get("max_posts_per_run", max_posts))
        to_post = fresh[:first_posts] if first_run else fresh[:limit]
        log(f"  {len(fresh)} new jobs not yet posted → posting {len(to_post)} this run")
        if first_run:
            log(f"  first run for this channel: the other {len(fresh) - len(to_post)} are marked as seen")
            for j in fresh[len(to_post):]:
                for k in (j["id"], j["dedupe"], j["ukey"]):
                    seen[sk(k)] = {"ch": key, "t": NOW.isoformat()}
        queues[key] = to_post

    # Digest channels: one compact message (or a few) per run, grouped by country.
    # The bot runs hourly but only posts every `digest_every_hours` (or sooner if many jobs pile up);
    # jobs that wait are not marked as seen, so the next run picks them up again.
    every = float(s.get("digest_every_hours", 3))
    early = int(s.get("digest_post_early_at", 30))
    last_post = state.setdefault("last_post", {})
    for key in [k for k in order if queues.get(k) and channels[k].get("format", "digest") == "digest"]:
        ch = channels[key]
        jobs = queues.pop(key)
        since = (NOW - parse_date(last_post[key])).total_seconds() / 3600 if last_post.get(key) else 999
        if since < every - 0.25 and len(jobs) < early and "--now" not in sys.argv:
            log(f"  {key}: {len(jobs)} new jobs waiting; last digest {since:.1f}h ago, next in "
                f"~{every - since:.1f}h (or at {early} jobs)")
            continue
        for text in digest_messages(jobs, ch):
            if dry:
                log(f"\n--- DRY RUN DIGEST [{key}] ---\n" + text)
                ok = True
            else:
                ok = send(token, ch["chat_id"], text)
                time.sleep(1.5)
            if not ok:
                break
        else:
            for j in jobs:
                for k in (j["id"], j["dedupe"], j["ukey"]):
                    seen[f"{key}::{k}"] = {"ch": key, "t": NOW.isoformat()}
            last_post[key] = NOW.isoformat()
            log(f"  {key}: posted a digest of {len(jobs)} jobs")

    # "Card" channels: one detailed post per job, round-robin across channels
    active = [k for k in order if queues.get(k)]
    gap = max(0.4, delay / max(1, len(active)))
    posted = {k: 0 for k in active}
    while any(queues.get(k) for k in active):
        for key in active:
            if not queues[key]:
                continue
            j = queues[key].pop(0)
            ch = channels[key]
            text = format_post(j, ch)
            if dry:
                log(f"\n--- DRY RUN POST [{key}] ---\n" + text)
                ok = True
            else:
                ok = send(token, ch["chat_id"], text)
                time.sleep(gap)
            if ok:
                for k in (j["id"], j["dedupe"], j["ukey"]):
                    seen[f"{key}::{k}"] = {"ch": key, "t": NOW.isoformat()}
                posted[key] += 1
    if posted:
        log("\nPosted: " + ", ".join(f"{k} {n}" for k, n in posted.items()))

    # forget entries older than remember_days
    cutoff = NOW - timedelta(days=int(s.get("remember_days", 180)))
    state["seen"] = {k: v for k, v in seen.items() if (parse_date(v.get("t")) or NOW) > cutoff}

    if check:
        ok = sorted(k for k, v in state["platforms"].items() if v["ats"] != "none")
        bad = sorted(k for k, v in state["platforms"].items() if v["ats"] == "none")
        log(f"\nWorking ({len(ok)}): {', '.join(ok)}\nNot found ({len(bad)}): {', '.join(bad)}")
    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
