"""
Openings bot — fetches new jobs from free sources and posts them to Telegram channels.

Usage:
  python main.py            # normal run (posts to Telegram)
  python main.py --dry-run  # prints posts instead of sending, doesn't save state
  python main.py --check    # tests every company slug and prints which ones work

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


ATS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_company(slug, name, state):
    """Use the cached job-board type, or try all three and remember which works."""
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
        except Exception as e:
            log(f"  ! {slug} ({ats}) error: {e}")
            jobs = None
        if jobs is not None:
            cache[slug] = {"ats": ats, "checked": NOW.isoformat()}
            return jobs
    if known:  # cached platform stopped working → re-detect next run
        cache.pop(slug, None)
        return []
    cache[slug] = {"ats": "none", "checked": NOW.isoformat()}
    log(f"  ✗ '{slug}' not found on Greenhouse/Lever/Ashby — fix or remove it in sources.yaml")
    return []


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
                       | {"_text": " ".join([j.get("position", ""), " ".join(j.get("tags") or [])])})
        _remote_cache["remoteok"] = out
    return _remote_cache["remoteok"]


def fetch_remotive(state):
    # Remotive asks for no more than ~4 requests per day.
    last = parse_date(state.get("remotive_last"))
    if last and NOW - last < timedelta(hours=6):
        return []
    if "remotive" not in _remote_cache:
        r = HTTP.get("https://remotive.com/api/remote-jobs", timeout=TIMEOUT)
        data = r.json().get("jobs", []) if r.ok else []
        out = []
        for j in data:
            out.append(job("rmt", j.get("id"), j.get("title"), j.get("company_name"),
                           j.get("candidate_required_location") or "Remote", j.get("url"),
                           j.get("publication_date"), j.get("salary") or None, remote=True,
                           description=j.get("description", ""))
                       | {"_text": " ".join([j.get("title", ""), j.get("category", ""),
                                             " ".join(j.get("tags") or [])])})
        _remote_cache["remotive"] = out
        state["_remotive_fetched"] = True
    return _remote_cache["remotive"]


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
    ("BD", ["business development", "bd ", "partnership", "sales", "account executive", "account manager", "institutional", "relationship manager", "growth"], "BD and sales people"),
    ("Marketing", ["marketing", "community", "content", "social media", "brand", "communications", "pr ", "seo", "ecosystem", "devrel", "developer relations", "events", "kol"], "marketers and community builders"),
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


def has_any(text, words):
    t = f" {text.lower()} "
    return any(w in t for w in words)


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


_gemini_model = None


def pick_gemini_model(key):
    """Ask Google which models this key can use; pick the newest stable 'flash' model."""
    global _gemini_model
    if _gemini_model is not None:
        return _gemini_model
    _gemini_model = os.environ.get("GEMINI_MODEL") or ""
    if _gemini_model:
        return _gemini_model
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
    ranked = sorted(flash, key=lambda n: (("lite" not in n), ("preview" not in n), ver(n), -len(n)),
                    reverse=True)
    _gemini_model = ranked[0] if ranked else ""
    log(f"  Gemini model: {_gemini_model or 'none available'}")
    return _gemini_model


def who_ai(j):
    """One-sentence 'who should apply' via Google Gemini free tier. Returns None on any issue."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not j["description"]:
        return None
    model = pick_gemini_model(key)
    if not model:
        return None
    prompt = (
        "You write one line for a job-board post. Based on the job below, say WHO SHOULD APPLY: "
        "the ideal candidate's background, years of experience, and 2-3 key skills. "
        "Max 30 words, plain text, no emojis, no intro like 'Ideal candidate:'. "
        "Start with the type of person (e.g. 'Backend engineers with 5+ years...').\n\n"
        f"Title: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
        f"Description:\n{j['description'][:4000]}"
    )
    try:
        r = HTTP.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key}, timeout=60,
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}})
        if not r.ok:
            log(f"  ! Gemini {r.status_code}: {r.text[:300]}")
            return None
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        text = re.sub(r"\s+", " ", text).strip(' "')
        return text if 10 < len(text) < 300 else None
    except Exception as e:
        log(f"  ! Gemini error: {e}")
        return None


def format_post(j, ch):
    e = html.escape
    func, _, _, places = classify(j)
    who = who_ai(j) or who_rule_based(j)
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
        f'🔗 <a href="{e(j["url"], quote=True)}">Apply here</a>',
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

def collect(ch, state):
    jobs = []
    for slug, name in (ch.get("companies") or {}).items():
        got = fetch_company(slug, name or slug, state)
        if got:
            log(f"  ✓ {slug}: {len(got)} jobs")
        jobs += got
    kw = ch.get("keywords") or []
    extra = []
    try:
        if ch.get("remoteok"):
            extra += fetch_remoteok()
    except Exception as e:
        log(f"  ! RemoteOK error: {e}")
    try:
        if ch.get("remotive"):
            extra += fetch_remotive(state)
    except Exception as e:
        log(f"  ! Remotive error: {e}")
    for url in ch.get("rss") or []:
        try:
            extra += fetch_rss(url)
        except Exception as e:
            log(f"  ! RSS error {url}: {e}")
    kept = [j for j in extra if not kw or matches(j.get("_text") or j["title"], kw)]
    log(f"  remote/RSS: {len(kept)} of {len(extra)} match keywords")
    return jobs + kept


def main():
    dry = "--dry-run" in sys.argv
    check = "--check" in sys.argv
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    s = cfg.get("settings", {})
    max_posts = int(s.get("max_posts_per_run", 2))
    max_age = timedelta(days=int(s.get("max_job_age_days", 10)))
    first_posts = int(s.get("first_run_posts", 3))
    delay = float(s.get("delay_between_posts", 4))

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    seen = state.setdefault("seen", {})
    if check:
        state["platforms"] = {}  # force re-detection

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token and not (dry or check):
        sys.exit("TELEGRAM_BOT_TOKEN is not set")

    for key, ch in cfg["channels"].items():
        log(f"\n=== {key} → {ch['chat_id']} ===")
        jobs = collect(ch, state)
        if check:
            continue
        first_run = not any(v.get("ch") == key for v in seen.values())

        fresh, batch_keys = [], set()
        for j in jobs:
            if not j["url"] or not j["title"]:
                continue
            ukey = "url:" + j["url"].split("?")[0].rstrip("/").lower()
            if (j["id"] in seen or j["dedupe"] in seen or ukey in seen
                    or j["dedupe"] in batch_keys or ukey in batch_keys):
                continue
            if j["posted"] and NOW - j["posted"] > max_age:
                continue
            batch_keys.update([j["dedupe"], ukey])
            j["ukey"] = ukey
            fresh.append(j)
        fresh.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        log(f"  {len(fresh)} new jobs not yet posted")

        to_post = fresh[:first_posts] if first_run else fresh[:max_posts]
        if first_run:
            log(f"  first run: posting {len(to_post)}, marking the rest as seen")
            for j in fresh[len(to_post):]:
                seen[j["id"]] = seen[j["dedupe"]] = seen[j["ukey"]] = {"ch": key, "t": NOW.isoformat()}

        for j in to_post:
            text = format_post(j, ch)
            if dry:
                log("\n--- DRY RUN POST ---\n" + text)
                ok = True
            else:
                ok = send(token, ch["chat_id"], text)
                time.sleep(delay)
            if ok:
                seen[j["id"]] = seen[j["dedupe"]] = seen[j["ukey"]] = {"ch": key, "t": NOW.isoformat()}
                log(f"  posted: {j['company']} — {j['title']}")

    # remember Remotive fetch time, prune old entries (60 days)
    if state.pop("_remotive_fetched", None):
        state["remotive_last"] = NOW.isoformat()
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
