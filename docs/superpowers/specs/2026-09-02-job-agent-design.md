# Job Agent — Design Specification

## Overview

A Python-based job scraping agent that discovers tech job postings from ATS (Applicant Tracking System) public APIs, stores them in a local database, presents them in a web dashboard, and sends batched Slack notifications for new listings.

**Goal:** Automatically find and surface new tech job postings (internships, new grad, entry-level, etc.) across hundreds of companies, with configurable filters and real-time phone notifications.

**Out of scope (v1):** Application autofill, resume tailoring, AI job matching, referral connections.

---

## 1. Architecture

```
┌─────────────────────────────────────────────────┐
│                  Web Dashboard                   │
│            (FastAPI + Jinja2 + HTMX)            │
│   Browse · Filter · Search · Save jobs          │
└────────────────────┬────────────────────────────┘
                     │ reads/writes
┌────────────────────▼────────────────────────────┐
│              SQLite Database                     │
│   jobs · companies · notified_jobs · preferences │
└────────────────────┬────────────────────────────┘
                     │ fed by
┌────────────────────▼────────────────────────────┐
│           Scraper Engine (Python)                │
│  Greenhouse · Lever · Ashby · (expandable)       │
│  Company discovery · Job normalization            │
│  Runs hourly via APScheduler                     │
└────────────────────┬────────────────────────────┘
                     │ notifies via
┌────────────────────▼────────────────────────────┐
│         Notification Service                     │
│     Slack webhook — batched digest format        │
└─────────────────────────────────────────────────┘
```

### Key technology choices

| Layer | Technology | Rationale |
|---|---|---|
| Backend | FastAPI (Python) | Async support, auto-docs, lightweight |
| Frontend | Jinja2 + HTMX | Server-rendered, no build step, minimal JS. HTMX adds dynamic filtering without React/Vue overhead |
| Database | SQLite | Zero config, single file, sufficient for 1M+ rows |
| Scheduling | APScheduler | Runs inside the FastAPI process, no separate cron needed |
| Notifications | Slack webhook | Simple HTTP POST, mobile push via Slack app |
| Packaging | pyproject.toml + uv | Modern Python packaging |

---

## 2. Data Model

### `companies` table
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| name | TEXT NOT NULL | Display name (e.g. "Stripe") |
| slug | TEXT NOT NULL | ATS identifier (e.g. "stripe") |
| ats_platform | TEXT NOT NULL | "greenhouse", "lever", or "ashby" |
| ats_board_url | TEXT | e.g. `https://boards.greenhouse.io/stripe` |
| career_url | TEXT | Company career page URL |
| last_scraped_at | DATETIME | When we last fetched jobs |
| is_active | BOOLEAN | Default 1; set to 0 if ATS returns errors |
| UNIQUE | (slug, ats_platform) | |

### `jobs` table
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| company_id | INTEGER FK | References companies.id |
| external_id | TEXT | ID from the ATS API |
| title | TEXT NOT NULL | Job title |
| location | TEXT | e.g. "San Francisco, CA" or "Remote" |
| remote_type | TEXT | "remote", "hybrid", "onsite" |
| experience_level | TEXT | "internship", "new_grad", "entry", "mid", "senior" — auto-classified from title |
| category | TEXT | "swe", "data_science", "pm", "design", "quant", "other" — auto-classified from title |
| description | TEXT | Full job description (HTML or plain text) |
| apply_url | TEXT NOT NULL | Direct link to application page |
| salary_min | INTEGER | If available from ATS |
| salary_max | INTEGER | If available from ATS |
| salary_currency | TEXT | e.g. "USD" |
| departments | TEXT | JSON array of department names |
| first_seen_at | DATETIME | Default CURRENT_TIMESTAMP |
| is_active | BOOLEAN | Default 1; set to 0 when job disappears from ATS |
| UNIQUE | (company_id, external_id) | |

### `notified_jobs` table
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| job_id | INTEGER FK | References jobs.id |
| notified_at | DATETIME | Default CURRENT_TIMESTAMP |
| batch_id | TEXT | Groups jobs notified in the same Slack message |

### `preferences` table
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| key | TEXT UNIQUE | Preference name |
| value | TEXT | JSON-encoded value |

---

## 3. Scraper Engine

### ATS Adapters

Each platform has a Python adapter implementing a common interface:

```python
class ATSAdapter(ABC):
    platform: str

    async def discover_companies(self) -> list[CompanyInfo]: ...
    async def fetch_jobs(self, company_slug: str) -> list[RawJob]: ...
```

**API endpoints used:**

| Platform | Endpoint | Auth |
|---|---|---|
| Greenhouse | `GET https://api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | None |
| Lever | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | None |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | None |

### Normalization

Each adapter maps platform-specific fields to a common schema:

| Common Field | Greenhouse | Lever | Ashby |
|---|---|---|---|
| title | `title` | `text` | `title` |
| url | `absolute_url` | `hostedUrl` | `jobUrl` |
| location | `location.name` | `categories.location` | `locationName` |
| description | `content` | `descriptionPlain` | `jobDescription` |

### Experience level classification

Auto-classified from job title using keyword matching:

| Level | Keywords |
|---|---|
| internship | intern, co-op |
| new_grad | new grad, recent grad |
| entry | junior, entry level, associate, level 1, level i |
| mid | mid level, mid-level, level 2, level ii, 2-5 years |
| senior | senior, staff, principal, lead, level 3+, 5+ years |

### Category classification

| Category | Keywords |
|---|---|
| swe | software engineer, software developer, backend, frontend, full stack, fullstack, SRE, devops, platform engineer |
| data_science | data scientist, data analyst, machine learning, ML engineer, AI engineer |
| pm | product manager, program manager, technical program manager |
| design | product designer, UX, UI, graphic designer |
| quant | quant, quantitative, researcher, algo |

### Company discovery

Two-phase:
1. **Seed list** — `seed_companies.json` shipped with the project, containing ~100 well-known tech companies with their ATS slugs and platforms
2. **Discovery CLI** — `python -m jobagent discover` command that optionally crawls ATS board URL patterns to find new companies (disabled by default)

### Scraping flow (hourly)

1. Load company list from database (or seed file on first run)
2. For each ATS platform (in parallel, max 5 concurrent requests per platform):
   - For each company on that platform:
     - Fetch jobs via public API
     - Normalize to common Job schema
     - Classify experience_level and category from title
     - Upsert into SQLite (skip duplicates via UNIQUE constraint)
     - Track newly-seen job IDs for notifications
3. Mark jobs no longer returned by ATS as inactive (`is_active = 0`)
4. Batch new jobs by category
5. Send Slack digest if there are new jobs matching user preferences
6. Update `last_scraped_at` for each company

### Rate limiting

- Max 5 concurrent requests per platform (configurable)
- 500ms delay between sequential requests to the same platform
- Exponential backoff on 429/500 errors (1s, 2s, 4s, max 3 retries)

---

## 4. Web Dashboard

### Pages

#### Job Browser (`/`)
- Sortable table: Title, Company, Location, Level, Category, Posted, Apply link
- HTMX-powered filters (no page reload):
  - Experience level checkboxes
  - Category checkboxes
  - Location text search
  - Remote-only toggle
  - ATS platform filter
  - Active-only toggle
- Full-text search across title, company, description
- Pagination (50 per page)

#### Job Detail (`/jobs/{id}`)
- Full job description
- Company info sidebar + other open roles at that company
- Direct "Apply" button (opens ATS URL in new tab)
- Mark as Saved / Applied / Ignored

#### Stats (`/stats`)
- Total jobs tracked, jobs by platform, jobs by category
- Jobs added per day (simple chart)
- Top companies by job count
- Experience level distribution

#### Settings (`/settings`)
- Configure Slack webhook URL
- Set notification filters (which levels, categories to notify about)
- Trigger manual scrape
- View scraper status (last run, companies tracked, jobs found)

### Tech details
- Server-rendered HTML via Jinja2 templates
- HTMX for all interactive elements (filtering, pagination, search)
- Minimal CSS via a lightweight classless CSS framework (e.g. Pico CSS or Simple.css)
- No build step, no npm, no JavaScript bundler

---

## 5. Notifications

### Slack webhook integration
- Single HTTP POST per scrape cycle (only if new jobs found)
- Uses Slack Block Kit for rich formatting

### Digest format

```
🆕 12 New Jobs Found

── Internships (7) ──────────────────
• SWE Intern — Stripe (Remote)
• ML Intern — Anthropic (SF)
• Frontend Intern — Vercel (Remote)
• ...

── New Grad (3) ──────────────────
• New Grad SWE — OpenAI (SF)
• ...

── Entry Level (2) ──────────────────
• Junior Backend — PostHog (Remote)
• ...
```

### Notification rules
- Only notify about jobs matching current preference filters
- Skip jobs already in `notified_jobs` table
- If 0 new matching jobs, send nothing (no empty notifications)
- Each job in the digest links directly to the apply URL

---

## 6. Configuration

### `config.yaml`

```yaml
scraping:
  interval_minutes: 60
  max_concurrent_requests: 5
  request_delay_ms: 500

ats_platforms:
  greenhouse:
    enabled: true
  lever:
    enabled: true
  ashby:
    enabled: true

notifications:
  slack_webhook_url: ""  # Or set via SLACK_WEBHOOK_URL env var
  enabled: true
  filters:
    experience_levels: ["internship"]
    categories: ["swe", "data_science", "pm"]

dashboard:
  host: "0.0.0.0"
  port: 8000
```

### Environment variables (override config.yaml)
- `SLACK_WEBHOOK_URL` — Slack webhook URL
- `DATABASE_PATH` — SQLite database file path (default: `data/jobs.db`)
- `CONFIG_PATH` — Config file path (default: `config.yaml`)

---

## 7. Project Structure

```
job-agent/
├── src/
│   └── jobagent/
│       ├── __init__.py
│       ├── __main__.py          # Entry point: python -m jobagent
│       ├── config.py            # Config loading (yaml + env vars)
│       ├── database.py          # SQLite setup, migrations, queries
│       ├── models.py            # Pydantic models for Job, Company, etc.
│       ├── scraper/
│       │   ├── __init__.py
│       │   ├── base.py          # ATSAdapter abstract class
│       │   ├── greenhouse.py    # Greenhouse adapter
│       │   ├── lever.py         # Lever adapter
│       │   ├── ashby.py         # Ashby adapter
│       │   └── engine.py        # Scraper orchestrator (scheduling, rate limiting)
│       ├── classifier.py        # Experience level + category classification
│       ├── notifier.py          # Slack webhook integration
│       ├── web/
│       │   ├── __init__.py      # FastAPI app setup
│       │   ├── routes.py        # Dashboard routes
│       │   └── templates/       # Jinja2 HTML templates
│       │       ├── base.html
│       │       ├── jobs.html
│       │       ├── job_detail.html
│       │       ├── stats.html
│       │       └── settings.html
│       └── cli.py               # CLI commands (seed, discover, serve)
├── data/
│   ├── seed_companies.json      # Pre-configured company list
│   └── jobs.db                  # SQLite database (gitignored)
├── config.yaml                  # User configuration
├── config.example.yaml          # Example config (committed)
├── pyproject.toml
├── README.md
└── .gitignore
```

---

## 8. Error Handling

- **ATS API errors (4xx/5xx):** Log warning, mark company as erroring, skip and continue
- **Rate limiting (429):** Exponential backoff, max 3 retries, then skip
- **Database errors:** Log and continue (don't crash the scraper loop)
- **Slack webhook failure:** Log warning, job data is still saved (notifications are non-critical)
- **Malformed job data:** Skip individual jobs, log which company/ATS caused the issue

All errors are logged but never crash the hourly scrape cycle. The system is designed to be resilient — partial failures don't stop the whole pipeline.

---

## 9. Testing Strategy

- **Unit tests** for classifiers (experience level + category from title)
- **Unit tests** for ATS adapter normalization (mock API responses → common schema)
- **Integration tests** for database operations (CRUD, dedup, inactive marking)
- **Integration tests** for notification batching logic
- **Manual testing** for the web dashboard (HTMX interactions)

---

## 10. Future Considerations (v2+)

These are explicitly out of scope for v1 but inform design decisions:
- More ATS platforms (Workable, Personio, Recruitee)
- Company discovery pipeline (Common Crawl integration)
- Email notifications (in addition to Slack)
- Job application tracking (save/apply/ignore states)
- AI-powered job matching and resume tailoring
- Multi-user support (different filter preferences per user)
