# job-alerts

Personal job-alert watcher. Every ~5 minutes (GitHub Actions), it polls
ToS-permitted job sources for new **analyst** roles in target locations and
posts them to a private Discord webhook.

## Sources (all permit automated read access)
- **Greenhouse** public job-board API (`boards-api.greenhouse.io`)
- **Lever** public postings API (`api.lever.co/v0/postings`)
- **JPMorgan Chase** via Oracle Recruiting Cloud's public candidate-experience
  REST API (the same unauthenticated JSON API their careers site uses).
  JPMC postings are flagged **high priority**.

**Never** polls LinkedIn or Indeed. **Never** submits applications.

## Filters
- Title contains "analyst" (Business / Data / Category / Supply Chain /
  Procurement / plain Analyst, any level).
- Location: Greater Toronto Area, or US states NY, CA, OR, WA, MA, AZ, FL, NC
  ("Washington, DC" is excluded from the WA match).
- US postings get a work-authorization flag scanned from the job description:
  `No sponsorship mentioned` / `Sponsorship/TN visa OK` /
  `Must be authorized without sponsorship`.

## Config
- `companies.json` — Greenhouse/Lever boards to watch. Add a company by adding
  its board token; the next run picks it up (its postings older than
  `MAX_AGE_HOURS` are seeded silently, not alerted).
- `state/seen.json` — dedup state, committed back by the workflow.
- Secret `DISCORD_WEBHOOK_URL` — the Discord webhook (repo secret).
- Env knobs: `MAX_AGE_HOURS` (default 72), `MAX_ALERTS_PER_RUN` (default 12),
  `DRY_RUN=1` (print instead of sending, state untouched).

## Run locally
```
DISCORD_WEBHOOK_URL=... python3 watcher.py
```
