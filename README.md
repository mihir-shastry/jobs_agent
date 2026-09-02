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

## Design doc

See [`docs/superpowers/specs/2026-09-02-job-agent-design.md`](docs/superpowers/specs/2026-09-02-job-agent-design.md) for the full architecture, data model, and testing strategy.
