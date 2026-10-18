"""
Cross-posts each Telegram digest to free social networks, to bring new people to the channels.

Supported (each one switches on by itself when its GitHub secrets exist):
  Bluesky   BLUESKY_HANDLE (e.g. openingsjobs.bsky.social) + BLUESKY_APP_PASSWORD
  Mastodon  MASTODON_INSTANCE (e.g. https://mastodon.social) + MASTODON_TOKEN

One short post per channel per digest: a few top roles + a link to the Telegram channel and website.
Any error is logged and skipped, so social posting can never stop the Telegram posts.
"""
import os
import re
from datetime import datetime, timezone

import requests

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "OpeningsJobsBot/1.0"


def _tg(ch):
    return "https://t.me/" + str(ch.get("chat_id", "")).lstrip("@")


def pick_roles(jobs, group_of, n):
    """Remote and Singapore roles first, one per company."""
    prio = {"Remote": 0, "Singapore": 1}
    ranked = sorted(jobs, key=lambda j: prio.get(group_of(j)[0], 2))
    out, seen = [], set()
    for j in ranked:
        c = j["company"].lower()
        if c in seen:
            continue
        seen.add(c)
        out.append(j)
        if len(out) >= n:
            break
    return out


def role_line(j, group_of, width):
    label = group_of(j)[0]
    where = "" if label in ("Other locations",) else f" ({label})"
    line = f"• {j['company']}: {j['title']}{where}"
    return line if len(line) <= width else line[:width - 1] + "…"


def compose(jobs, ch, group_of, site_url, limit, roles, width):
    name = ch.get("title", "jobs")
    n = len(jobs)
    head = f"🗂 {n} new {name} just posted"
    tags = [ch.get("hashtag", "").strip(), "#Hiring", "#Jobs"]
    if any(group_of(j)[0] == "Remote" for j in jobs):
        tags.append("#RemoteJobs")
    tail_tg = f"Get them on Telegram 👉 {_tg(ch)}"
    for k in range(roles, 0, -1):
        lines = [role_line(j, group_of, width) for j in pick_roles(jobs, group_of, k)]
        more = n - len(lines)
        body = "\n".join([head, ""] + lines + ([f"+{more} more"] if more > 0 else []) + ["", tail_tg])
        if site_url:
            body += f"\nBrowse all: {site_url}"
        full = body + "\n\n" + " ".join(t for t in tags if t)
        if len(full) <= limit:
            return full
        if len(body) <= limit:
            return body
    return f"{head}\n{tail_tg}"[:limit]


# ───────────── Bluesky ─────────────
def _facets(text):
    """Clickable links and hashtags (Bluesky needs byte offsets)."""
    b = text.encode("utf-8")
    facets = []
    for m in re.finditer(rb"https?://[^\s]+", b):
        facets.append({"index": {"byteStart": m.start(), "byteEnd": m.end()},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group().decode()}]})
    for m in re.finditer(rb"(?:^|\s)(#[A-Za-z][A-Za-z0-9_]*)", b):
        s, e = m.start(1), m.end(1)
        facets.append({"index": {"byteStart": s, "byteEnd": e},
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": b[s + 1:e].decode()}]})
    return facets


_bsky = {}


def post_bluesky(text, card, log):
    handle, pw = os.environ.get("BLUESKY_HANDLE"), os.environ.get("BLUESKY_APP_PASSWORD")
    if not (handle and pw):
        return None
    if not _bsky:
        r = HTTP.post("https://bsky.social/xrpc/com.atproto.server.createSession",
                      json={"identifier": handle.lstrip("@"), "password": pw}, timeout=20)
        if not r.ok:
            log(f"  ! Bluesky login failed {r.status_code}: {r.text[:150]}")
            return False
        _bsky.update(r.json())
    record = {"$type": "app.bsky.feed.post", "text": text, "facets": _facets(text), "langs": ["en"],
              "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    if card:
        record["embed"] = {"$type": "app.bsky.embed.external", "external": card}
    r = HTTP.post("https://bsky.social/xrpc/com.atproto.repo.createRecord",
                  headers={"Authorization": f"Bearer {_bsky['accessJwt']}"},
                  json={"repo": _bsky["did"], "collection": "app.bsky.feed.post", "record": record}, timeout=20)
    if not r.ok:
        log(f"  ! Bluesky post failed {r.status_code}: {r.text[:150]}")
        return False
    return True


# ───────────── Mastodon ─────────────
def post_mastodon(text, log):
    inst, tok = os.environ.get("MASTODON_INSTANCE"), os.environ.get("MASTODON_TOKEN")
    if not (inst and tok):
        return None
    inst = inst.strip().rstrip("/")
    if not inst.startswith("http"):
        inst = "https://" + inst
    r = HTTP.post(f"{inst}/api/v1/statuses", headers={"Authorization": f"Bearer {tok}"},
                  data={"status": text, "visibility": "public", "language": "en"}, timeout=20)
    if not r.ok:
        log(f"  ! Mastodon post failed {r.status_code}: {r.text[:150]}")
        return False
    return True


def share_digest(jobs, ch, key, cfg, group_of, log=print):
    """Called after a digest was posted to Telegram."""
    s = cfg.get("social") or {}
    if s.get("enabled") is False or not jobs:
        return
    base = ((cfg.get("site") or {}).get("base_url") or "").rstrip("/")
    site_url = f"{base}/{key}/" if base and (cfg.get("site") or {}).get("enabled", True) else ""
    done = []
    try:
        text = compose(jobs, ch, group_of, "", 290, 3, 70)  # Bluesky: 300 characters max
        card = {"uri": _tg(ch), "title": f"Openings · {ch.get('title', 'jobs')} on Telegram",
                "description": "Fresh jobs from official company career pages, 3× a day. No spam, no duplicates."}
        ok = post_bluesky(text, card, log)
        if ok:
            done.append("Bluesky")
    except Exception as ex:
        log(f"  ! Bluesky error: {ex}")
    try:
        ok = post_mastodon(compose(jobs, ch, group_of, site_url, 490, 5, 80), log)
        if ok:
            done.append("Mastodon")
    except Exception as ex:
        log(f"  ! Mastodon error: {ex}")
    if done:
        log(f"  {key}: shared on {', '.join(done)}")
