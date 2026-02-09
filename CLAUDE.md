# Cheap Finder

A price tracking system for fashion brands across Canadian retailers.

## Project Overview

Track prices for curated fashion brands across all Canadian retailers that carry them. The system discovers which retailers sell each brand, monitors prices daily, stores price history, and sends alerts when prices drop or items go on sale.

## Tech Stack

All tools and libraries are free and open-source. No paid services, APIs, or subscriptions required.

- **Language:** Python 3.12+
- **Web Framework:** FastAPI (async API + lightweight dashboard)
- **Database:** SQLite via SQLAlchemy (simple, no server needed, zero cost; migrate to PostgreSQL if scale demands it)
- **Scraping:** httpx + BeautifulSoup4 for static pages; Playwright for JS-rendered pages (all free/OSS)
- **Scheduling:** APScheduler for daily price checks (runs locally, no cloud scheduler needed)
- **Notifications:** Email via free SMTP (Gmail App Password or similar free provider) + in-dashboard notification center
- **Frontend:** Jinja2 templates + HTMX for a lightweight interactive dashboard (no SPA framework needed)
- **CSS:** Tailwind CSS (via free CDN) — dark mode default with light mode toggle
- **Charts:** Chart.js (free/OSS) for price history graphs
- **Hosting:** Runs locally on your machine. No cloud hosting costs. Optionally deploy free on Railway/Render free tier or a Raspberry Pi.
- **Fonts:** Inter via Google Fonts (free)

## Project Structure

```
Cheap_finder/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env                     # secrets (SMTP creds, webhook URLs) — never commit
├── .gitignore
├── alembic/                 # DB migrations
│   └── versions/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entrypoint
│   ├── config.py            # settings loaded from .env
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py        # SQLAlchemy models
│   │   ├── session.py       # DB engine/session setup
│   │   └── migrations.py
│   ├── brands/
│   │   ├── __init__.py
│   │   ├── registry.py      # brand definitions and metadata
│   │   └── discovery.py     # find which retailers carry a brand
│   ├── retailers/
│   │   ├── __init__.py
│   │   ├── base.py          # abstract retailer scraper
│   │   ├── simons.py
│   │   ├── ssense.py
│   │   ├── nordstrom.py
│   │   ├── sporting_life.py
│   │   ├── altitude_sports.py
│   │   ├── haven.py
│   │   ├── livestock.py
│   │   ├── nrml.py
│   │   └── generic.py       # fallback scraper for simpler sites
│   ├── tracking/
│   │   ├── __init__.py
│   │   ├── price_checker.py # orchestrates daily price checks
│   │   ├── history.py       # price history queries and trends
│   │   └── scheduler.py     # APScheduler setup
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── rules.py         # alert conditions (% drop, sale detection)
│   │   └── notifier.py      # email/webhook dispatch
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes_brands.py
│   │   ├── routes_products.py
│   │   ├── routes_alerts.py
│   │   └── routes_dashboard.py
│   └── templates/
│       ├── base.html             # layout shell, nav, dark/light toggle, notification bell
│       ├── dashboard.html        # overview: tracked brands, recent drops, top deals
│       ├── brand_detail.html     # brand page: products grid with thumbnails, price history
│       ├── product_detail.html   # single product: price chart, retailer comparison table
│       ├── alerts.html           # manage alert rules per brand
│       ├── notifications.html    # notification center (unread/read)
│       └── components/
│           ├── product_card.html # reusable product card with thumbnail, price, sale badge
│           ├── price_chart.html  # Chart.js price history component
│           └── nav.html          # top nav with dark mode toggle + notification badge
└── tests/
    ├── conftest.py
    ├── test_brands/
    ├── test_retailers/
    ├── test_tracking/
    └── test_alerts/
```

## Data Models (Core)

**Brand** — name, slug, aliases (e.g. "A.P.C." / "APFR"), category tags, active flag
**Retailer** — name, base_url, scraper_type, requires_js flag
**BrandRetailer** — maps brand ↔ retailer (the discovery result)
**Product** — name, brand_id, url, retailer_id, image_url, thumbnail_url, sku, tracked flag
**PriceRecord** — product_id, price, currency, sale_flag, timestamp
**AlertRule** — user-defined: brand/product scope, condition (% drop threshold configurable per brand), notification channels (email + dashboard)
**AlertEvent** — when a rule fires: rule_id, product_id, old_price, new_price, timestamp, sent_email flag, seen_dashboard flag
**Notification** — in-dashboard notification: alert_event_id, title, message, read flag, created_at

## Initial Brands

| Brand | Aliases / Notes |
|---|---|
| On Cloud | "On Running" |
| Satisfy Running | "Satisfy" |
| A.P.C. | "APFR", "A.P.C" |
| New Balance | Track Made in USA + Made in UK lines specifically |
| Balmoral | — |
| Arc'teryx | "Arcteryx" |
| Sabre Paris | "Sabre" |

## Initial Retailers

| Retailer | URL | Notes |
|---|---|---|
| Simons | simons.ca | |
| SSENSE | ssense.com | JS-heavy, may need Playwright |
| Nordstrom | nordstrom.ca | |
| Sporting Life | sportinglife.ca | |
| Altitude Sports | altitude-sports.com | |
| Haven | havenshop.com | |
| Livestock | deadstock.ca | |
| NRML | nrml.ca | |
| Brand direct sites | *.ca or regional | per-brand |

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run the API server (dev)
uvicorn src.main:app --reload --port 8000

# Run price checks manually
python -m src.tracking.price_checker

# Run tests
pytest tests/ -v

# Run a single retailer scraper for debugging
python -m src.retailers.ssense --brand "Arc'teryx" --dry-run

# DB migrations
alembic upgrade head
alembic revision --autogenerate -m "description"
```

## UI / Design

- **Theme:** Dark mode default, light mode toggle. Persist choice in localStorage.
- **Aesthetic:** Clean, minimal, modern — inspired by SSENSE/Haven. Generous whitespace, sharp typography, subtle borders.
- **Typography:** Inter (Google Fonts) — clean sans-serif that works in both themes.
- **Color palette (dark):** background `#0a0a0a`, surface `#141414`, border `#262626`, text `#fafafa`, muted `#a1a1aa`, accent green for price drops `#22c55e`, accent red for price increases `#ef4444`.
- **Color palette (light):** background `#fafafa`, surface `#ffffff`, border `#e5e5e5`, text `#0a0a0a`, same accent colors.
- **Product cards:** Thumbnail image (lazy-loaded, aspect-ratio preserved), brand name, product name, current price, original price with strikethrough if on sale, retailer badge, sale percentage badge.
- **Dashboard layout:** Top nav (logo left, dark/light toggle + notification bell right). Main content is responsive grid. Cards, tables, and charts with subtle hover effects.
- **Charts:** Chart.js with theme-aware colors. Line charts for price history. Clean gridlines, no chart junk.
- **Notification bell:** Badge with unread count. Dropdown on click showing recent alerts. Link to full notifications page.
- **Responsive:** Mobile-friendly grid that collapses to single column. Touch-friendly tap targets.
- **Tailwind config:** Use CDN with inline config for custom colors. Dark mode via `class` strategy (not `media`).

## Coding Conventions

- Use `async/await` for all I/O (HTTP requests, DB queries)
- Type hints on all function signatures
- Each retailer scraper inherits from `RetailerBase` in `src/retailers/base.py`
- Scrapers must implement: `search_brand(brand) -> list[Product]` and `get_price(product_url) -> PriceRecord`
- Use `httpx.AsyncClient` for HTTP; fall back to Playwright only when JS rendering is required
- Prices stored as integers in cents (CAD) to avoid float issues
- All timestamps in UTC
- Use `logging` module, not print statements
- Tests use pytest + pytest-asyncio; use fixtures for DB and mock HTTP responses
- Keep scraper logic isolated — no business logic in scraper files

## Scraping Guidelines

- Respect `robots.txt` and rate-limit requests (minimum 2s between requests to same domain)
- Rotate User-Agent strings
- Cache responses during development to avoid hammering retailers
- Handle common failure modes: product removed, price not found, page layout changed
- Store raw HTML snapshots for debugging scraper failures (configurable, off by default)
- Each scraper should have a `health_check()` method that verifies the scraper still works against a known product

## Key Design Decisions

- **SQLite first** — no need for PostgreSQL until we outgrow single-machine. WAL mode for concurrent reads.
- **One scraper per retailer** — retailers change layouts independently; isolating scrapers makes maintenance easier.
- **Brand discovery is semi-manual** — initial brand→retailer mapping is seeded manually, with optional automated discovery via site search pages.
- **Prices in cents** — `4999` not `49.99`. Currency is always CAD unless explicitly noted.
- **No user auth for v1** — single-user system. Add auth later if needed.
- **Dark mode default** — Tailwind dark mode with toggle to light. Persist preference in localStorage.
- **Product images** — scrape thumbnail URLs from retailers; store URLs in DB, lazy-load in dashboard with fallback placeholder.
- **Per-brand alert thresholds** — each brand has its own configurable % drop threshold. Default 10% if not set.
- **Dual notifications** — every alert fires both email + in-dashboard notification. Dashboard shows unread count badge.
- **Zero cost** — everything runs locally with free tools. No paid APIs, no subscriptions, no cloud bills.

## Environment Variables (.env)

```
DATABASE_URL=sqlite+aiosqlite:///./cheapfinder.db

# Email alerts — use Gmail with App Password (free) or any free SMTP provider
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=                    # your Gmail address
SMTP_PASS=                    # Gmail App Password (not your regular password)
ALERT_EMAIL_TO=               # where to receive alerts

# Optional free webhook notifications
DISCORD_WEBHOOK_URL=          # free — create in Discord server settings
SLACK_WEBHOOK_URL=            # free for personal workspaces

LOG_LEVEL=INFO
PRICE_CHECK_HOUR=6            # hour (UTC) to run daily checks
REQUEST_DELAY_SECONDS=2
SAVE_HTML_SNAPSHOTS=false
```

## Free Tools Constraint

This project uses **zero paid services**. Every component must be free:
- No paid APIs (no ScraperAPI, no Oxylabs, no paid proxy services)
- No paid databases (SQLite is free, PostgreSQL is free if self-hosted)
- No paid email services (use Gmail App Password, free tier of Resend, or similar)
- No paid hosting required (runs locally; free tier hosting optional)
- No paid monitoring (use Python logging + dashboard)
- All Python packages are OSS and free (PyPI)
- Tailwind, HTMX, Chart.js, Inter font — all free via CDN / Google Fonts
- If a scraper needs a proxy to avoid blocks, use free rotation techniques (delay, User-Agent rotation) not paid proxy services

## Deployment (Render Free Tier)

The app is configured to deploy on Render's free tier:

### Quick deploy steps
1. Push the repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` and configures everything
5. Add secret env vars in the Render dashboard (SMTP creds, etc.)
6. Set `RENDER_EXTERNAL_URL` to your service URL (e.g. `https://cheap-finder.onrender.com`)

### Key files
- `render.yaml` — Render Blueprint (auto-configures the service)
- `/health` endpoint — used by Render health checks

### Keep-alive
Render free tier spins down after 15 min of inactivity. The app includes a self-ping mechanism:
- When `RENDER_EXTERNAL_URL` is set, a background job pings `/health` every 10 minutes
- This keeps the instance warm so the scheduler can run on time

### Important notes
- **SQLite on Render:** The filesystem resets on each deploy. Data persists between pings/requests but is lost on redeploy. For persistent data, migrate to PostgreSQL (Render offers a free PostgreSQL instance).
- **Free tier limits:** 750 hours/month (enough for always-on), spins down after 15 min idle (solved by keep-alive), ~512 MB RAM.
- **Build command:** Installs dependencies + Playwright Chromium for JS-heavy sites.

## Git Workflow

- `main` branch is stable
- Feature branches: `feature/<short-description>`
- Commit messages: imperative mood, concise (e.g. "Add Ssense scraper", "Fix price parsing for Nordstrom sale items")

## TODO / Roadmap

- [x] Project scaffolding (pyproject.toml, directory structure, DB setup)
- [x] Define SQLAlchemy models and create initial migration
- [x] Build base retailer scraper class
- [x] Brand registry with initial brand list
- [x] Daily price check scheduler
- [x] Price history storage and basic trend queries
- [x] Alert rules engine and email notifications
- [x] Dashboard (list brands, view price history charts, manage alerts)
- [x] Render free tier deployment config with keep-alive
- [ ] Implement first 2-3 retailer scrapers (start with Simons, Altitude Sports — simpler sites)
- [ ] Add remaining retailer scrapers
- [ ] Brand discovery automation (search retailer sites for brand presence)
- [ ] Migrate to Render PostgreSQL for persistent data
