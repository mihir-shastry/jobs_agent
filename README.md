# Job Agent

Scrapes tech job postings from ATS public APIs (Greenhouse, Lever, Ashby), stores them in SQLite, serves a filterable web dashboard, and sends batched Slack digest notifications for new listings.

## How it works

```
Scraper Engine (hourly) ──► SQLite ──► Web Dashboard (FastAPI + HTMX)
        │
        └──► Slack digest (new jobs grouped by level/category)
```

- **Sources:** public, unauthenticated ATS job-board APIs — no scraping of LinkedIn/Indeed, no accounts, no keys.
- **Auto-classification:** every job gets an experience level (internship → senior) and category (swe, data_science, pm, …) derived from its title.
- **Digest notifications:** one grouped Slack message per scrape cycle, only for jobs matching your filters.

## Quick start

```bash
# 1. Install (uses uv; creates .venv with Python 3.12)
uv sync

# 2. Configure
cp config.example.yaml config.yaml
#    → set notifications.slack_webhook_url (or export SLACK_WEBHOOK_URL)

# 3. Seed the starter company list (~100 tech companies)
uv run python -m jobagent seed-companies

# 4. Run: dashboard at http://127.0.0.1:8000, scraper starts with it
uv run python -m jobagent serve

# 5. (Optional) scrape once right now without the server
uv run python -m jobagent scrape
```

## CLI

| Command | Description |
|---|---|
| `python -m jobagent serve` | Start dashboard + hourly scraper |
| `python -m jobagent scrape` | Run one scrape cycle now and exit |
| `python -m jobagent seed-companies` | Load `data/seed_companies.json` into the DB |
| `python -m jobagent discover` | Probe candidate slugs and add boards that respond |
| `python -m jobagent stats` | Print job/company counts |

## Configuration

`config.yaml` (copy from `config.example.yaml`). Environment variables override it:

| Env var | Overrides |
|---|---|
| `SLACK_WEBHOOK_URL` | `notifications.slack_webhook_url` |
| `JOBAGENT_DB_PATH` | `database.path` |
| `JOBAGENT_CONFIG_PATH` | location of the config file itself |

## Getting a Slack webhook

1. Create a Slack app at https://api.slack.com/apps → *From scratch*, pick your workspace.
2. Enable **Incoming Webhooks** and add one for your channel (e.g. `#jobs`).
3. Copy the `https://hooks.slack.com/services/...` URL into `config.yaml` or `export SLACK_WEBHOOK_URL=...`.

## Free hosting: GitHub Actions + Pages

The agent can run entirely outside your Mac for free: a scheduled GitHub Actions workflow scrapes hourly, sends the Slack digest to your phone, commits a small state file, and publishes a static dashboard to GitHub Pages. No credit card, no server.

### One-time setup

1. **Push this repo to GitHub** (public works best — free Actions minutes are unlimited on public repos).
2. **Add the Slack secret:** repo → *Settings* → *Secrets and variables* → *Actions* → *New repository secret* → name `SLACK_WEBHOOK_URL`, value = your webhook URL.
3. **Enable Pages:** repo → *Settings* → *Pages* → *Build and deployment* → Source: **GitHub Actions**.
4. Trigger the first run: repo → *Actions* → "Scrape jobs and deploy dashboard" → *Run workflow*. (The hourly cron also self-disables after 60 days with no repo activity — the workflow's own commits keep it alive.)

After the first run, your dashboard is live at `https://<user>.github.io/<repo>/`.

### What changes vs. running locally

| | Local (`serve`) | GitHub Actions (`scrape-portable`) |
|---|---|---|
| Storage | SQLite (`data/jobs.db`) | Temp DB per run + committed `data/seen_jobs.jsonl` (seen-keys, pruned after 30 days) |
| Dashboard | Interactive FastAPI/HTMX app | Static `docs/index.html` reading `docs/jobs.json` (search, filters, statuses via localStorage) |
| Notifications | Batched Slack digest | Identical batched Slack digest |
| Company list | `data/seed_companies.json` | Same file — edit it and the next run picks it up |

The portable path reuses the same adapters, classifier, and notifier as local mode; only delivery and state differ.

### Local dry run

You can exercise the exact CI pipeline locally:

```bash
uv run python -m jobagent scrape-portable
# writes data/seen_jobs.jsonl and docs/jobs.json
```

## Design doc

See [`docs/superpowers/specs/2026-09-02-job-agent-design.md`](docs/superpowers/specs/2026-09-02-job-agent-design.md) for the full architecture, data model, and testing strategy.
