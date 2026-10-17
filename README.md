# Openings bot

Posts new jobs to Telegram channels every 3 hours, for free, using GitHub Actions.

## Files
- `main.py` — fetches, de-duplicates, formats and posts jobs
- `sources.yaml` — channels, companies and feeds (edit this one)
- `.github/workflows/run.yml` — the schedule
- `seen_jobs.json` — created automatically; remembers what was already posted

## Run modes (Actions → Post jobs → Run workflow)
- `check` — tests every company slug and lists which ones work
- `dry-run` — prints the posts in the log, sends nothing
- `normal` — posts to Telegram

## Add a company
Find its careers page. If the URL looks like
`boards.greenhouse.io/NAME`, `jobs.lever.co/NAME` or `jobs.ashbyhq.com/NAME`,
add `NAME: Display Name` under `companies:` in `sources.yaml`.

## Add a channel
Copy the commented `ai:` block at the bottom of `sources.yaml`, create the
Telegram channel, make it public and add the bot as an admin.
