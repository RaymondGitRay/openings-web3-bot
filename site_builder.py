"""
Builds the Openings job website (static HTML) from the jobs the bot collected this run.

- One page per job with schema.org JobPosting data, so Google's job search can list it.
- Listing pages per channel, per country and per job function.
- sitemap.xml + robots.txt for search engines.
Every page invites readers to join the matching Telegram channel.

Output goes to ./site and is published by the workflow to GitHub Pages (nothing is committed).
"""
import html
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

OUT = Path(__file__).parent / "site"
NOW = datetime.now(timezone.utc)
e = html.escape

COUNTRY_CODES = {
    "Singapore": "SG", "Hong Kong": "HK", "UAE": "AE", "India": "IN", "Japan": "JP", "South Korea": "KR",
    "Taiwan": "TW", "China": "CN", "Malaysia": "MY", "Indonesia": "ID", "Philippines": "PH", "Vietnam": "VN",
    "Thailand": "TH", "Australia": "AU", "New Zealand": "NZ", "USA": "US", "Canada": "CA", "UK": "GB",
    "Ireland": "IE", "Germany": "DE", "France": "FR", "Switzerland": "CH", "Netherlands": "NL",
    "Portugal": "PT", "Spain": "ES", "Poland": "PL", "Austria": "AT", "Italy": "IT", "Israel": "IL",
    "Turkey": "TR", "Brazil": "BR", "Mexico": "MX", "Argentina": "AR", "Nigeria": "NG", "South Africa": "ZA",
}
FUNCTION_NAMES = {
    "Engineering": "Engineering", "Data": "Data & AI", "Security": "Security", "Product": "Product",
    "Design": "Design", "Compliance": "Compliance & Risk", "Finance": "Finance", "BD": "BD & Sales",
    "Marketing": "Marketing", "CustomerSuccess": "Customer Success", "Operations": "Operations",
    "People": "People & Talent", "Intern": "Internships",
}

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#14171c;--muted:#5d6673;--line:#e3e6eb;--accent:#0f766e;
--accent-ink:#fff;--chip:#eef2f4;--tg:#229ed9}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e8ebef;--muted:#9aa4b1;
--line:#262c34;--accent:#2dd4bf;--accent-ink:#06201d;--chip:#1f252d;--tg:#3aaee0}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:inherit}a:hover{color:var(--accent)}
.wrap{max-width:860px;margin:0 auto;padding:0 16px}
header.top{border-bottom:1px solid var(--line);background:var(--card)}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:12px;height:58px}
.brand{font-weight:800;font-size:19px;text-decoration:none;letter-spacing:-.01em}
.brand span{color:var(--accent)}
nav a{margin-left:14px;text-decoration:none;color:var(--muted);font-weight:600;font-size:15px}
h1{font-size:clamp(26px,5vw,36px);line-height:1.15;margin:28px 0 8px;letter-spacing:-.02em}
h2{font-size:20px;margin:30px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:11px 18px;border-radius:10px;font-weight:700;
text-decoration:none;border:0;cursor:pointer;font-size:15px}
.btn.apply{background:var(--accent);color:var(--accent-ink)}
.btn.tg{background:var(--tg);color:#fff}
.btn:hover{filter:brightness(1.07);color:inherit}.btn.tg:hover{color:#fff}.btn.apply:hover{color:var(--accent-ink)}
.cta{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:22px 0;
display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.cta p{margin:0;max-width:520px}.cta b{display:block;font-size:17px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 4px}
.chip{background:var(--chip);border-radius:999px;padding:5px 12px;font-size:14px;text-decoration:none;
white-space:nowrap}
.chip small{color:var(--muted);margin-left:4px}
ul.jobs{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);
border-radius:14px;overflow:hidden}
ul.jobs li{border-top:1px solid var(--line)}ul.jobs li:first-child{border-top:0}
ul.jobs a{display:block;padding:13px 16px;text-decoration:none}
ul.jobs a:hover{background:var(--chip)}
.jt{font-weight:650;display:block}.jm{color:var(--muted);font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h3{margin:0 0 4px}.card p{margin:0 0 14px;color:var(--muted)}
.meta{display:flex;flex-wrap:wrap;gap:8px 18px;color:var(--muted);margin:6px 0 18px;font-size:15px}
.who{background:var(--chip);border-radius:12px;padding:12px 14px;margin:18px 0}
.desc{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 20px;
overflow-wrap:anywhere}
.desc p{margin:12px 0}
.crumbs{font-size:14px;color:var(--muted);margin-top:20px}.crumbs a{text-decoration:none}
.note{color:var(--muted);font-size:14px}
footer{margin:40px 0 0;border-top:1px solid var(--line);padding:22px 0 40px;color:var(--muted);font-size:14px}
.sticky{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 35%);padding:18px 16px 14px;
display:flex;gap:10px;flex-wrap:wrap}
"""


def slugify(s, n=60):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-") or "x"


def stable_hash(s):
    h = 5381
    for c in s:
        h = (h * 33 + ord(c)) & 0xFFFFFFFF
    return format(h, "x")[-6:]


def tg_link(ch):
    return "https://t.me/" + str(ch.get("chat_id", "")).lstrip("@")


def page(base, title, desc, canonical, body, ch=None, jsonld=None, noindex=False):
    root = urlparse(base).path
    tg = ch or {}
    head_extra = ""
    if jsonld:
        head_extra += ('<script type="application/ld+json">'
                       + json.dumps(jsonld, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    if noindex:
        head_extra += '<meta name="robots" content="noindex">'
    sticky = ""
    if tg.get("chat_id"):
        sticky = (f'<div class="wrap sticky"><a class="btn tg" href="{tg_link(tg)}" rel="noopener">'
                  f'✈️ Get {e(tg.get("title", "jobs"))} on Telegram</a></div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(canonical)}"><meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}"><meta property="og:type" content="website">
<meta property="og:url" content="{e(canonical)}"><meta name="theme-color" content="#0f766e">
<link rel="stylesheet" href="{root}assets/style.css">{head_extra}</head><body>
<header class="top"><div class="wrap"><a class="brand" href="{root}">Openings<span>.</span></a>
<nav>{NAV}</nav></div></header>
<main class="wrap">{body}</main>{sticky}
<footer><div class="wrap">Openings · fresh jobs from official company career pages, updated every hour.
Listings link to the employer's own application page. Join us on Telegram: {TG_FOOTER}</div></footer>
</body></html>"""


NAV = ""
TG_FOOTER = ""


def job_line(j, root):
    meta = " · ".join(x for x in (j["company"], j.get("location") or "", j["_age"]) if x)
    return (f'<li><a href="{root}jobs/{j["_slug"]}.html"><span class="jt">{e(j["title"])}</span>'
            f'<span class="jm">{e(meta)}</span></a></li>')


def age_text(d):
    if not d:
        return ""
    days = (NOW - d).days
    return "today" if days <= 0 else ("1 day ago" if days == 1 else f"{days} days ago")


def paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    return "".join(f"<p>{e(p)}</p>" for p in parts)


def build(site_jobs, channels, cfg, group_of, classify, who_lines=None, log=print):
    """site_jobs: {channel_key: [job, ...]} for enabled channels. Returns number of job pages."""
    global NAV, TG_FOOTER
    s = (cfg.get("site") or {})
    if s.get("enabled") is False:
        log("  website: disabled in sources.yaml")
        return 0
    base = s.get("base_url", "").rstrip("/") + "/"
    root = urlparse(base).path
    max_age = timedelta(days=int(s.get("max_job_age_days", 60)))
    list_cap = int(s.get("max_jobs_per_list", 400))
    who_lines = who_lines or {}

    live = {k: ch for k, ch in channels.items() if k in site_jobs}
    NAV = "".join(f'<a href="{root}{k}/">{e(ch.get("title", k).replace(" jobs", ""))}</a>'
                  for k, ch in live.items())
    TG_FOOTER = " · ".join(f'<a href="{tg_link(ch)}">{e(ch["chat_id"])}</a>' for ch in live.values())

    # One record per unique job (a job can belong to several channels; the first one "owns" its page)
    jobs, by_channel = {}, {}
    for key, lst in site_jobs.items():
        keep = []
        for j in lst:
            if not j.get("url") or not j.get("title") or not j.get("company"):
                continue
            if j.get("posted") and NOW - j["posted"] > max_age:
                continue
            d = j["dedupe"]
            if d not in jobs:
                r = dict(j)
                r["_slug"] = f'{slugify(j["company"], 30)}-{slugify(j["title"], 60)}-{stable_hash(d)}'
                r["_age"] = age_text(j.get("posted"))
                r["_group"] = group_of(j)
                f = classify(j)[0]
                r["_func"] = f[0] if f else ""
                r["_ch"] = key
                jobs[d] = r
            keep.append(jobs[d])
        uniq = {id(x): x for x in keep}.values()
        by_channel[key] = sorted(uniq, key=lambda r: (r.get("posted") or datetime.min.replace(tzinfo=timezone.utc)),
                                 reverse=True)

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    (OUT / "jobs").mkdir()
    (OUT / "assets" / "style.css").write_text(CSS.strip())
    (OUT / ".nojekyll").write_text("")
    urls = []

    def write(rel, content):
        p = OUT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    # ── job pages ──
    for d, j in jobs.items():
        ch = live[j["_ch"]]
        label, flag, _tag = j["_group"]
        url = f'{base}jobs/{j["_slug"]}.html'
        meta = [f'🏢 {e(j["company"])}']
        if j.get("location"):
            meta.append(f'📍 {e(j["location"])}')
        if j.get("salary"):
            meta.append(f'💰 {e(str(j["salary"]))}')
        if j["_age"]:
            meta.append(f'🗓 Posted {j["_age"]}')
        who = who_lines.get(d)
        same = [x for x in by_channel[j["_ch"]] if x["company"] == j["company"] and x is not j][:6]
        chips = (f'<div class="chips"><a class="chip" href="{root}{j["_ch"]}/{slugify(label)}/">{flag} '
                 f'{e(label)} {e(ch.get("title", ""))}</a>')
        if j["_func"]:
            chips += (f'<a class="chip" href="{root}{j["_ch"]}/{slugify(j["_func"])}/">'
                      f'{e(FUNCTION_NAMES.get(j["_func"], j["_func"]))}</a>')
        chips += "</div>"
        body = (f'<div class="crumbs"><a href="{root}">Openings</a> › <a href="{root}{j["_ch"]}/">'
                f'{e(ch.get("title", ""))}</a> › <a href="{root}{j["_ch"]}/{slugify(label)}/">{e(label)}</a></div>'
                f'<h1>{e(j["title"])}</h1><div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>'
                f'{chips}<p><a class="btn apply" href="{e(j["url"], quote=True)}" rel="nofollow noopener" '
                f'target="_blank">Apply on the official page ↗</a></p>')
        if who:
            body += f'<div class="who"><b>🎯 Who should apply:</b> {e(who)}</div>'
        body += (f'<div class="cta"><p><b>Get jobs like this 3× a day</b>New {e(ch.get("title", "jobs"))} '
                 f'from top companies, posted to Telegram. No spam, no duplicates.</p>'
                 f'<a class="btn tg" href="{tg_link(ch)}" rel="noopener">Join {e(ch["chat_id"])}</a></div>')
        if j.get("description"):
            body += f'<h2>About the role</h2><div class="desc">{paragraphs(j["description"])}</div>'
            body += ('<p class="note">Description from the employer\'s listing. Always apply via the official '
                     'page above.</p>')
        if same:
            body += (f'<h2>More jobs at {e(j["company"])}</h2><ul class="jobs">'
                     + "".join(job_line(x, root) for x in same) + "</ul>")

        jsonld = None
        country = COUNTRY_CODES.get(label)
        if j.get("posted") and ((j.get("remote") and country) or (not j.get("remote") and j.get("location"))):
            jsonld = {
                "@context": "https://schema.org/", "@type": "JobPosting",
                "title": j["title"],
                "description": paragraphs(j.get("description") or j["title"]),
                "datePosted": j["posted"].date().isoformat(),
                "validThrough": (j["posted"] + max_age).isoformat(),
                "hiringOrganization": {"@type": "Organization", "name": j["company"]},
                "identifier": {"@type": "PropertyValue", "name": j["company"], "value": j["id"]},
                "directApply": False,
            }
            if j.get("remote"):
                jsonld["jobLocationType"] = "TELECOMMUTE"
                jsonld["applicantLocationRequirements"] = {"@type": "Country", "name": country}
            else:
                addr = {"@type": "PostalAddress",
                        "addressLocality": (j.get("location") or label).split(",")[0].strip()[:80]}
                if country:
                    addr["addressCountry"] = country
                jsonld["jobLocation"] = {"@type": "Place", "address": addr}
        title = f'{j["title"]} at {j["company"]}' + (f' ({j["location"]})' if j.get("location") else "")
        desc = (f'{j["company"]} is hiring a {j["title"]}' + (f' in {j["location"]}' if j.get("location") else "")
                + f'. Apply on the official page and get more {ch.get("title", "jobs")} on Telegram.')
        write(f'jobs/{j["_slug"]}.html', page(base, title + " | Openings", desc[:300], url, body, ch, jsonld))
        urls.append((url, j.get("posted")))

    # ── listing pages ──
    def listing(rel, ch, h1, sub, items, extra=""):
        url = base + rel
        shown = items[:list_cap]
        body = (f'<h1>{e(h1)}</h1><p class="sub">{e(sub)}</p>'
                f'<div class="cta"><p><b>Never miss a new opening</b>Fresh {e(ch.get("title", "jobs"))} '
                f'posted to Telegram 3× a day.</p><a class="btn tg" href="{tg_link(ch)}" rel="noopener">'
                f'Join {e(ch["chat_id"])}</a></div>{extra}<ul class="jobs">'
                + "".join(job_line(x, root) for x in shown) + "</ul>")
        if len(items) > len(shown):
            body += f'<p class="note">Showing the {len(shown)} newest of {len(items)} roles.</p>'
        write(rel + "index.html", page(base, f"{h1} | Openings", sub[:300], url, body, ch))
        urls.append((url, NOW))

    for key, ch in live.items():
        items = by_channel.get(key, [])
        name = ch.get("title", key)
        groups, funcs = {}, {}
        for x in items:
            groups.setdefault(x["_group"], []).append(x)
            if x["_func"]:
                funcs.setdefault(x["_func"], []).append(x)
        gsorted = sorted(groups.items(), key=lambda kv: (kv[0][0] != "Remote", kv[0][0] != "Singapore", -len(kv[1])))
        chips = ('<h2>By location</h2><div class="chips">' + "".join(
            f'<a class="chip" href="{root}{key}/{slugify(g[0])}/">{g[1]} {e(g[0])}<small>{len(v)}</small></a>'
            for g, v in gsorted) + "</div>")
        chips += ('<h2>By role</h2><div class="chips">' + "".join(
            f'<a class="chip" href="{root}{key}/{slugify(f)}/">{e(FUNCTION_NAMES.get(f, f))}<small>{len(v)}</small></a>'
            for f, v in sorted(funcs.items(), key=lambda kv: -len(kv[1]))) + "</div><h2>Latest roles</h2>")
        listing(f"{key}/", ch, name[:1].upper() + name[1:],
                f"{len(items)} open {name} from official company career pages, updated every hour.",
                items, chips)
        for (label, flag, _), v in groups.items():
            h = f"Remote {name}" if label == "Remote" else f"{name[:1].upper() + name[1:]} in {label}"
            listing(f"{key}/{slugify(label)}/", ch, h,
                    f"{len(v)} open {name} {'(remote)' if label == 'Remote' else 'in ' + label}, updated every hour.",
                    v)
        for f, v in funcs.items():
            fname = FUNCTION_NAMES.get(f, f)
            listing(f"{key}/{slugify(f)}/", ch, f"{fname} · {name}",
                    f"{len(v)} open {fname.lower()} roles in {name.lower()}, updated every hour.", v)

    # ── home page ──
    allj = sorted(jobs.values(), key=lambda r: (r.get("posted") or datetime.min.replace(tzinfo=timezone.utc)),
                  reverse=True)
    cards = "".join(
        f'<div class="card"><h3>{e(ch.get("title", k))[:1].upper() + e(ch.get("title", k))[1:]}</h3>'
        f'<p>{len(by_channel.get(k, []))} open roles</p><a class="btn tg" href="{tg_link(ch)}" rel="noopener">'
        f'Join {e(ch["chat_id"])}</a> <a class="chip" href="{root}{k}/">Browse →</a></div>'
        for k, ch in live.items())
    body = (f'<h1>Find your next role in Web3 &amp; Tech</h1><p class="sub">{len(jobs):,} open jobs from '
            f'official company career pages, updated every hour. Get the new ones on Telegram, 3× a day.</p>'
            f'<div class="cards">{cards}</div><h2>Latest roles</h2><ul class="jobs">'
            + "".join(job_line(x, root) for x in allj[:60]) + "</ul>")
    write("index.html", page(base, "Openings · Web3 & Tech jobs, updated hourly",
                             "Fresh Web3 and tech jobs from official company career pages, with Telegram alerts.",
                             base, body))
    urls.append((base, NOW))
    write("404.html", page(base, "Page not found | Openings", "This job may have closed.", base,
                           f'<h1>This page is gone</h1><p class="sub">The job may have closed. '
                           f'<a href="{root}">See the latest openings →</a></p>', noindex=True))

    # ── sitemap + robots ──
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, d in urls[:50000]:
        sm.append(f"<url><loc>{e(u)}</loc><lastmod>{(d or NOW).date().isoformat()}</lastmod></url>")
    sm.append("</urlset>")
    write("sitemap.xml", "\n".join(sm))
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n")
    if s.get("google_verification"):
        write(s["google_verification"], f'google-site-verification: {s["google_verification"]}')
    log(f"  website: {len(jobs)} job pages, {len(urls) - len(jobs)} other pages → {base}")
    return len(jobs)
