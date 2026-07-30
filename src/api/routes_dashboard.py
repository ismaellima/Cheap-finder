from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from collections import Counter
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.responses import StreamingResponse
from starlette.status import HTTP_303_SEE_OTHER

from src.config import settings
from src.db.models import (
    AlertRule,
    Brand,
    BrandRetailer,
    Notification,
    PriceRecord,
    Product,
    Retailer,
    RetailerSuggestion,
)
from src.db.session import async_session, get_session
from src.brands.rematch import rematch_brand_products, trigger_rediscovery

logger = logging.getLogger(__name__)

# In-memory progress tracking for retailer discovery tasks.
# Key: "retailer-{id}", Value: progress dict.
# Entries are ephemeral — they exist only while the server is running.
_discovery_progress: Dict[str, Dict[str, Any]] = {}


def _cleanup_stale_progress() -> None:
    """Remove progress entries older than 5 minutes."""
    cutoff = time.time() - 300
    stale = [k for k, v in _discovery_progress.items() if v.get("updated_at", 0) < cutoff]
    for k in stale:
        del _discovery_progress[k]


# Strong references to in-flight background jobs. The event loop only holds a
# weak reference to a running task, so a bare asyncio.create_task() whose result
# nobody keeps can be garbage collected mid-execution — it disappears with no
# exception and no log line. That is what made POST /price-check return 303 and
# then do nothing at all. Hold the task until it finishes, then drop it.
_background_tasks: set = set()


def _spawn(coro) -> None:
    """Run a coroutine in the background, keeping it alive until it completes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _finished(t) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background task failed", exc_info=t.exception())

    task.add_done_callback(_finished)


router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="src/templates")

# Make auth_enabled available in all templates (for logout/login button in nav)
templates.env.globals["auth_enabled"] = bool(settings.DASHBOARD_PASSWORD)


def _is_admin(request: Request) -> bool:
    """Check if the current request is from an authenticated admin user."""
    if not bool(settings.DASHBOARD_PASSWORD):
        return True  # No password set — everyone is admin
    return bool(request.session.get("authenticated"))


def _from_json(value: str) -> list:
    """Jinja2 filter: parse a JSON string into a Python list."""
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["from_json"] = _from_json


def format_price(cents: int | None) -> str:
    if cents is None:
        return "N/A"
    return f"${cents / 100:,.2f}"


def _discount_pct(product: Product) -> float:
    return (product.original_price - product.current_price) / product.original_price * 100


CATEGORY_COLORS = {
    "outdoor": "#2dd4bf",
    "fashion": "#a78bfa",
    "footwear": "#f59e0b",
    "sneakers": "#38bdf8",
    "running": "#fb923c",
    "home": "#f472b6",
    "perfume": "#e879f9",
}
DEFAULT_CATEGORY_COLOR = "#8a8a8a"

RAIL_DEALS_PER_BRAND = 2
RAIL_DEALS_PER_RETAILER = 2
RAIL_DEALS_TOTAL = 8

# The rail only shows drops whose price was actually re-confirmed this recently.
# Note this keys off PriceRecord, not Product.last_checked: last_checked is
# stamped on failed checks too (so the rolling batch can advance past
# unreadable products), whereas a PriceRecord row is only written when a fetch
# genuinely succeeded. Filtering on last_checked would therefore keep exactly
# the delisted products this is meant to exclude.
RAIL_VERIFIED_DAYS = 7


def select_rail_drops(candidates) -> list:
    """Pick the rail line-up from candidates already ordered best-first.

    Caps per brand *and* per retailer. A brand-only cap was not enough: a
    single outlet discounts across many brands, so it could still take every
    slot while never tripping the per-brand limit.
    """
    chosen: list = []
    per_brand: Dict[int, int] = {}
    per_retailer: Dict[int, int] = {}

    for product in candidates:
        if len(chosen) >= RAIL_DEALS_TOTAL:
            break
        if per_brand.get(product.brand_id, 0) >= RAIL_DEALS_PER_BRAND:
            continue
        if per_retailer.get(product.retailer_id, 0) >= RAIL_DEALS_PER_RETAILER:
            continue
        chosen.append(product)
        per_brand[product.brand_id] = per_brand.get(product.brand_id, 0) + 1
        per_retailer[product.retailer_id] = per_retailer.get(product.retailer_id, 0) + 1

    return chosen


DEALS_PAGE_SIZES = (8, 16, 24)
DEALS_MAX_PER_PAGE = 200


@router.get("/")
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    success: str = "",
    error: str = "",
):
    success_messages = {
        "brand_added": "Brand added! Product discovery is running in the background.",
        "brand_deleted": "Brand deleted.",
        "discovery_started": "Product discovery started in the background. Refresh in a few minutes to see results.",
    }
    error_messages = {
        "brand_empty_name": "Brand name cannot be empty.",
        "brand_duplicate": "A brand with this name already exists.",
        "brand_slug_taken": "A brand with a similar name already exists.",
        "brand_not_found": "Brand not found.",
        "product_not_found": "Product not found.",
    }

    brands_result = await session.execute(
        select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
    )
    brands = brands_result.scalars().all()

    # All on-sale products — feeds the stat strip, the capped "Today's Best
    # Drops" rail, and the per-brand deal-count badges below.
    drops_result = await session.execute(
        select(Product)
        .where(
            Product.on_sale.is_(True),
            Product.original_price.isnot(None),
            Product.original_price > 0,
            Product.current_price.isnot(None),
        )
        .options(selectinload(Product.brand), selectinload(Product.retailer))
        .order_by(Product.last_checked.desc().nullslast())
    )
    all_drops = drops_result.scalars().all()

    # Restrict the rail to drops we have actually re-confirmed recently.
    # Ranking every on-sale product by discount froze the rail solid: the top
    # slots were all held at the maximum discount by delisted outlet stock,
    # which can never be re-priced and never 404s, so no newly found drop could
    # ever displace them.
    verified_cutoff = dt.datetime.utcnow() - dt.timedelta(days=RAIL_VERIFIED_DAYS)
    verified_ids = set(
        (
            await session.execute(
                select(PriceRecord.product_id)
                .where(PriceRecord.recorded_at >= verified_cutoff)
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    drops_by_discount = sorted(
        (p for p in all_drops if p.id in verified_ids),
        key=_discount_pct,
        reverse=True,
    )

    rail_drops = select_rail_drops(drops_by_discount)

    deal_counts_by_brand = Counter(p.brand_id for p in all_drops)
    max_discount_pct = int(max((_discount_pct(p) for p in all_drops), default=0))

    # Unread notification count
    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    # Brand stats + retailer names — bulk queries to avoid N+1
    brand_ids = [b.id for b in brands]

    # Product counts per brand (one query)
    product_counts_result = await session.execute(
        select(Product.brand_id, func.count(Product.id).label("cnt"))
        .where(Product.brand_id.in_(brand_ids))
        .group_by(Product.brand_id)
    )
    product_counts = {row.brand_id: row.cnt for row in product_counts_result}

    # Retailer counts per brand (one query)
    retailer_counts_result = await session.execute(
        select(BrandRetailer.brand_id, func.count(BrandRetailer.id).label("cnt"))
        .where(BrandRetailer.brand_id.in_(brand_ids))
        .group_by(BrandRetailer.brand_id)
    )
    retailer_counts = {row.brand_id: row.cnt for row in retailer_counts_result}

    brand_stats = {
        b.id: {
            "product_count": product_counts.get(b.id, 0),
            "retailer_count": retailer_counts.get(b.id, 0),
        }
        for b in brands
    }

    # Retailer names per brand (one query)
    retailers_by_brand_result = await session.execute(
        select(BrandRetailer.brand_id, Retailer.name)
        .join(Retailer, BrandRetailer.retailer_id == Retailer.id)
        .where(BrandRetailer.brand_id.in_(brand_ids))
        .order_by(Retailer.name)
    )
    brand_retailers_map: dict[int, list[str]] = {b.id: [] for b in brands}
    for row in retailers_by_brand_result:
        brand_retailers_map[row.brand_id].append(row.name)

    total_products = sum(product_counts.values())
    categories = sorted({b.category for b in brands if b.category})
    brand_colors = {
        b.id: CATEGORY_COLORS.get(b.category.lower(), DEFAULT_CATEGORY_COLOR)
        for b in brands
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "brands": brands,
            "brand_stats": brand_stats,
            "brand_retailers_map": brand_retailers_map,
            "brand_colors": brand_colors,
            "deal_counts_by_brand": deal_counts_by_brand,
            "categories": categories,
            "rail_drops": rail_drops,
            "rail_verified_days": RAIL_VERIFIED_DAYS,
            "stats": {
                "total_products": total_products,
                "total_brands": len(brands),
                # Every product currently flagged on sale — not a per-day figure.
                # It was previously labelled "drops today", which it never was.
                "on_sale_now": len(all_drops),
                "max_discount_pct": max_discount_pct,
            },
            "unread_count": unread_count,
            "format_price": format_price,
            "success_message": success_messages.get(success, ""),
            "error_message": error_messages.get(error, ""),
            "is_admin": _is_admin(request),
        },
    )


@router.get("/deals")
async def deals_page(
    request: Request,
    brand: int = 0,
    sort: str = "discount",
    page: int = 1,
    per_page: int = 8,
    session: AsyncSession = Depends(get_session),
):
    """Full, filterable, paginated list of every on-sale product."""
    per_page = max(1, min(per_page, DEALS_MAX_PER_PAGE))

    query = (
        select(Product)
        .where(
            Product.on_sale.is_(True),
            Product.original_price.isnot(None),
            Product.original_price > 0,
            Product.current_price.isnot(None),
        )
        .options(selectinload(Product.brand), selectinload(Product.retailer))
    )
    if brand:
        query = query.where(Product.brand_id == brand)

    if sort == "price-asc":
        query = query.order_by(Product.current_price.asc())
    elif sort == "price-desc":
        query = query.order_by(Product.current_price.desc())
    elif sort == "recent":
        # "Most recent" = the last time we actually confirmed a price for this
        # product. Deliberately not Product.last_checked, which is stamped on
        # failed checks too and would float unreadable products to the top of
        # a list whose whole point is showing the freshest deals.
        latest_price = (
            select(
                PriceRecord.product_id.label("pid"),
                func.max(PriceRecord.recorded_at).label("last_seen"),
            )
            .group_by(PriceRecord.product_id)
            .subquery()
        )
        query = query.outerjoin(
            latest_price, latest_price.c.pid == Product.id
        ).order_by(latest_price.c.last_seen.desc().nullslast())
    else:
        sort = "discount"
        discount_expr = (
            (Product.original_price - Product.current_price) * 100.0 / Product.original_price
        )
        query = query.order_by(discount_expr.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total_products = (await session.execute(count_q)).scalar() or 0

    total_pages = max(1, (total_products + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    products_result = await session.execute(
        query.offset((page - 1) * per_page).limit(per_page)
    )
    products = products_result.scalars().all()

    # Brands that currently have at least one on-sale product — powers the filter chips.
    deal_brands_result = await session.execute(
        select(Brand.id, Brand.name, func.count(Product.id).label("cnt"))
        .join(Product, Product.brand_id == Brand.id)
        .where(
            Product.on_sale.is_(True),
            Product.original_price.isnot(None),
            Product.original_price > 0,
            Product.current_price.isnot(None),
        )
        .group_by(Brand.id, Brand.name)
        .order_by(Brand.name)
    )
    deal_brands = deal_brands_result.all()

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "deals.html",
        {
            "products": products,
            "deal_brands": deal_brands,
            "current_brand": brand,
            "current_sort": sort,
            "page_sizes": DEALS_PAGE_SIZES,
            "page": page,
            "total_pages": total_pages,
            "total_products": total_products,
            "per_page": per_page,
            "unread_count": unread_count,
            "format_price": format_price,
            "is_admin": _is_admin(request),
        },
    )


# --- Scraper status ---

# A product counts as "covered" if it was checked within one full cycle.
SCRAPER_CYCLE_HOURS = 24
# If anything was checked this recently, a batch is mid-run right now.
SCRAPER_ACTIVE_MINUTES = 5


def _humanise_delta(seconds: float) -> str:
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Drop tzinfo so APScheduler's aware times compare with our naive stamps."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


async def _scraper_stats(session: AsyncSession) -> dict:
    """Aggregate per-retailer scrape health in a single query.

    Success is defined as "this product's newest PriceRecord is at least as new
    as its last_checked stamp". A successful check writes the record and the
    stamp in the same transaction, so the record lands microseconds later; a
    failed check stamps last_checked and writes nothing, leaving the newest
    record far behind. Counting on last_checked alone cannot tell those apart —
    it advances either way, by design.
    """
    now = dt.datetime.utcnow()
    cycle_cutoff = now - dt.timedelta(hours=SCRAPER_CYCLE_HOURS)
    active_cutoff = now - dt.timedelta(minutes=SCRAPER_ACTIVE_MINUTES)

    newest_record = (
        select(
            PriceRecord.product_id.label("pid"),
            func.max(PriceRecord.recorded_at).label("newest"),
        )
        .group_by(PriceRecord.product_id)
        .subquery()
    )

    checked_recently = Product.last_checked >= cycle_cutoff
    succeeded = and_(checked_recently, newest_record.c.newest >= Product.last_checked)

    rows = (
        await session.execute(
            select(
                Retailer.id,
                Retailer.name,
                Retailer.scraper_type,
                func.count(Product.id).label("tracked"),
                func.sum(case((checked_recently, 1), else_=0)).label("checked"),
                func.sum(case((succeeded, 1), else_=0)).label("ok"),
                func.max(Product.last_checked).label("last_activity"),
            )
            .select_from(Retailer)
            .outerjoin(Product, Product.retailer_id == Retailer.id)
            .outerjoin(newest_record, newest_record.c.pid == Product.id)
            .where(Retailer.active.is_(True))
            .group_by(Retailer.id, Retailer.name, Retailer.scraper_type)
            .order_by(Retailer.name)
        )
    ).all()

    retailers = []
    tracked_total = checked_total = ok_total = 0
    last_activity: dt.datetime | None = None

    for row in rows:
        tracked = row.tracked or 0
        checked = int(row.checked or 0)
        ok = int(row.ok or 0)
        disabled = row.scraper_type in SKIP_SCRAPERS

        if not disabled:
            tracked_total += tracked
            checked_total += checked
            ok_total += ok
            if row.last_activity and (not last_activity or row.last_activity > last_activity):
                last_activity = row.last_activity

        retailers.append(
            {
                "name": row.name,
                "scraper_type": row.scraper_type,
                "tracked": tracked,
                "checked": checked,
                "ok": ok,
                "success_pct": round(ok / checked * 100) if checked else None,
                "coverage_pct": round(checked / tracked * 100) if tracked else None,
                "last_activity": row.last_activity,
                "disabled": disabled,
            }
        )

    # Retailers with the most products still unscrapable are the ones worth
    # fixing next, so surface them rather than burying them in alphabetical order.
    problems = sorted(
        (
            r
            for r in retailers
            if not r["disabled"] and r["checked"] and (r["success_pct"] or 0) < 50
        ),
        key=lambda r: (-(r["tracked"]), r["name"]),
    )

    next_run = None
    try:
        from src.tracking.scheduler import scheduler

        job = scheduler.get_job("rolling_price_check")
        if job is not None:
            next_run = getattr(job, "next_run_time", None)
    except Exception:  # scheduler not started (e.g. tests) — not worth failing over
        next_run = None

    return {
        "retailers": retailers,
        "problems": problems,
        "tracked_total": tracked_total,
        "checked_total": checked_total,
        "ok_total": ok_total,
        "failed_total": checked_total - ok_total,
        "coverage_pct": round(checked_total / tracked_total * 100, 1) if tracked_total else 0.0,
        "success_pct": round(ok_total / checked_total * 100, 1) if checked_total else None,
        "remaining": max(tracked_total - checked_total, 0),
        "running": bool(last_activity and last_activity >= active_cutoff),
        "last_activity": last_activity,
        "last_activity_ago": (
            _humanise_delta((now - last_activity).total_seconds()) if last_activity else None
        ),
        "next_run": _naive_utc(next_run),
        "next_run_in": (
            _humanise_delta((_naive_utc(next_run) - now).total_seconds())
            if _naive_utc(next_run) and _naive_utc(next_run) > now
            else None
        ),
        "cycle_hours": SCRAPER_CYCLE_HOURS,
        "batch_size": settings.PRICE_CHECK_BATCH_SIZE,
        "interval_minutes": settings.PRICE_CHECK_INTERVAL_MINUTES,
        "generated_at": now,
    }


@router.get("/scrapers")
async def scrapers_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    """Live view of how far through the cycle the price checks are."""
    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    return templates.TemplateResponse(
        request,
        "scrapers.html",
        {
            "stats": await _scraper_stats(session),
            "unread_count": unread_result.scalar() or 0,
            "is_admin": _is_admin(request),
        },
    )


@router.get("/scrapers/stats")
async def scrapers_stats_partial(
    request: Request, session: AsyncSession = Depends(get_session)
):
    """HTMX partial — the page polls this so the numbers stay live."""
    return templates.TemplateResponse(
        request,
        "components/scraper_stats.html",
        {"stats": await _scraper_stats(session)},
    )


# --- Add / Delete Brand ---


@router.post("/add-brand")
async def add_brand_submit(
    request: Request,
    name: str = Form(...),
    category: str = Form(""),
    alert_threshold_pct: float = Form(10.0),
    session: AsyncSession = Depends(get_session),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/?error=brand_empty_name", status_code=HTTP_303_SEE_OTHER)

    # Generate slug
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    # Check for duplicate name
    existing_name = await session.execute(
        select(Brand).where(Brand.name == name)
    )
    if existing_name.scalar_one_or_none():
        return RedirectResponse("/?error=brand_duplicate", status_code=HTTP_303_SEE_OTHER)

    # Check for duplicate slug
    existing_slug = await session.execute(
        select(Brand).where(Brand.slug == slug)
    )
    if existing_slug.scalar_one_or_none():
        return RedirectResponse("/?error=brand_slug_taken", status_code=HTTP_303_SEE_OTHER)

    # Create brand
    brand = Brand(
        name=name,
        slug=slug,
        aliases=json.dumps([]),
        category=category.strip(),
        alert_threshold_pct=alert_threshold_pct,
    )
    session.add(brand)
    await session.flush()

    # Create default alert rule
    rule = AlertRule(
        brand_id=brand.id,
        condition="pct_drop",
        threshold_pct=alert_threshold_pct,
        notify_email=True,
        notify_dashboard=True,
    )
    session.add(rule)
    await session.commit()

    # Trigger background discovery for the new brand
    brand_id = brand.id
    _spawn(_discover_brand_background(brand_id))

    return RedirectResponse("/?success=brand_added", status_code=HTTP_303_SEE_OTHER)


@router.post("/brands/{brand_id}/delete")
async def delete_brand_submit(
    request: Request,
    brand_id: int,
    session: AsyncSession = Depends(get_session),
):
    brand = await session.get(Brand, brand_id)
    if not brand:
        return RedirectResponse("/?error=brand_not_found", status_code=HTTP_303_SEE_OTHER)

    await session.delete(brand)
    await session.commit()

    return RedirectResponse("/?success=brand_deleted", status_code=HTTP_303_SEE_OTHER)


@router.post("/brands/{brand_id}/edit")
async def edit_brand_submit(
    request: Request,
    brand_id: int,
    name: str = Form(...),
    aliases: str = Form(""),
    category: str = Form(""),
    alert_threshold_pct: float = Form(10.0),
    session: AsyncSession = Depends(get_session),
):
    logger.info(f"Edit brand {brand_id}: name={name!r}, aliases={aliases!r}, category={category!r}")
    brand = await session.get(Brand, brand_id)
    if not brand:
        return RedirectResponse("/?error=brand_not_found", status_code=HTTP_303_SEE_OTHER)

    name = name.strip()
    if not name:
        return RedirectResponse(
            f"/brands/{brand_id}?error=brand_empty_name",
            status_code=HTTP_303_SEE_OTHER,
        )

    # If name changed, check uniqueness and regenerate slug
    if name != brand.name:
        existing_name = await session.execute(
            select(Brand).where(Brand.name == name, Brand.id != brand_id)
        )
        if existing_name.scalar_one_or_none():
            return RedirectResponse(
                f"/brands/{brand_id}?error=brand_duplicate",
                status_code=HTTP_303_SEE_OTHER,
            )

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        existing_slug = await session.execute(
            select(Brand).where(Brand.slug == slug, Brand.id != brand_id)
        )
        if existing_slug.scalar_one_or_none():
            return RedirectResponse(
                f"/brands/{brand_id}?error=brand_slug_taken",
                status_code=HTTP_303_SEE_OTHER,
            )

        brand.name = name
        brand.slug = slug

    # Parse aliases from comma-separated string
    # Handle None, empty string, or whitespace-only input
    old_aliases = json.loads(brand.aliases) if brand.aliases else []

    aliases_input = (aliases or "").strip()
    if aliases_input:
        alias_list = [a.strip() for a in aliases_input.split(",") if a.strip()]
    else:
        alias_list = []

    aliases_changed = set(old_aliases) != set(alias_list)

    brand.aliases = json.dumps(alias_list)
    logger.info(f"Setting aliases for brand {brand.name}: {alias_list}")

    brand.category = category.strip()
    brand.alert_threshold_pct = alert_threshold_pct

    # Update linked alert rule threshold
    rule_result = await session.execute(
        select(AlertRule).where(
            AlertRule.brand_id == brand_id,
            AlertRule.condition == "pct_drop",
        )
    )
    alert_rule = rule_result.scalar_one_or_none()
    if alert_rule:
        alert_rule.threshold_pct = alert_threshold_pct

    await session.commit()
    await session.refresh(brand)
    logger.info(f"Brand updated: {brand.name} (id={brand_id}), aliases={brand.aliases}")

    # Re-match products if aliases changed
    deleted_count = 0
    if aliases_changed:
        logger.info(
            f"Aliases changed for {brand.name}: {old_aliases} → {alias_list}"
        )

        # Delete existing products that may not match new aliases
        stats = await rematch_brand_products(session, brand)
        deleted_count = stats.get("deleted", 0)

        # Trigger re-discovery in background (don't wait)
        if deleted_count > 0 or alias_list:
            async def _safe_rediscovery():
                try:
                    await trigger_rediscovery(brand_id)
                except Exception as e:
                    logger.exception(f"Background re-discovery failed for brand {brand_id}")

            _spawn(_safe_rediscovery())

            logger.info(
                f"Queued re-discovery for {brand.name} after alias change "
                f"({deleted_count} products deleted)"
            )

    success_key = "brand_updated_rematching" if aliases_changed else "brand_updated"

    return RedirectResponse(
        f"/brands/{brand_id}?success={success_key}",
        status_code=HTTP_303_SEE_OTHER,
    )


SKIP_SCRAPERS = {"simons", "ssense", "nordstrom"}


def _get_working_scrapers() -> dict:
    """Get scrapers excluding known-broken ones."""
    from src.retailers import get_all_scrapers

    scrapers = get_all_scrapers()
    return {k: v for k, v in scrapers.items() if k not in SKIP_SCRAPERS}


async def _discover_brand_background(brand_id: int) -> None:
    """Run discovery for a single brand in the background."""
    from src.brands.discovery import discover_single_brand

    scrapers = _get_working_scrapers()
    try:
        async with async_session() as session:
            brand = await session.get(Brand, brand_id)
            if not brand:
                return
            stats = await discover_single_brand(session, brand, scrapers)
            logger.info(
                f"Background discovery for {brand.name}: "
                f"{stats['new_products']} products found"
            )
    except Exception:
        logger.exception(f"Background discovery failed for brand_id={brand_id}")
    finally:
        for s in scrapers.values():
            await s.close()


async def _discover_all_background() -> None:
    """Run full discovery for all brands in the background."""
    from src.brands.discovery import discover_and_store

    scrapers = _get_working_scrapers()
    try:
        async with async_session() as session:
            stats = await discover_and_store(session, scrapers)
            logger.info(
                f"Full discovery complete: {stats['new_products']} new products, "
                f"{stats['mappings_created']} mappings"
            )
    except Exception:
        logger.exception("Full background discovery failed")
    finally:
        for s in scrapers.values():
            await s.close()


async def _discover_retailer_background(retailer_id: int) -> None:
    """Run discovery for all brands at a single retailer, updating progress."""
    from src.brands.discovery import discover_brand_at_retailer, store_scraped_products
    from src.retailers import get_all_scrapers

    task_key = f"retailer-{retailer_id}"

    try:
        async with async_session() as session:
            retailer = await session.get(Retailer, retailer_id)
            if not retailer:
                _discovery_progress.pop(task_key, None)
                return

            all_scrapers = get_all_scrapers()
            scraper = all_scrapers.get(retailer.scraper_type)
            if not scraper:
                _discovery_progress.pop(task_key, None)
                return

            # Fetch all active brands
            brands_result = await session.execute(
                select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
            )
            brands = list(brands_result.scalars().all())

            # Initialize progress
            _discovery_progress[task_key] = {
                "status": "running",
                "current_brand": "",
                "brands_done": 0,
                "brands_total": len(brands),
                "products_found": 0,
                "new_products": 0,
                "message": "",
                "updated_at": time.time(),
            }

            total_products = 0
            total_new = 0

            try:
                for i, brand in enumerate(brands):
                    # Update progress: starting this brand
                    _discovery_progress[task_key].update({
                        "current_brand": brand.name,
                        "brands_done": i,
                        "updated_at": time.time(),
                    })

                    scraped = await discover_brand_at_retailer(
                        session, brand, retailer, scraper
                    )

                    brand_new = 0
                    if scraped:
                        total_products += len(scraped)
                        brand_new = await store_scraped_products(
                            session, brand, retailer, scraped
                        )
                        total_new += brand_new

                    # Update progress: finished this brand
                    _discovery_progress[task_key].update({
                        "brands_done": i + 1,
                        "products_found": total_products,
                        "new_products": total_new,
                        "updated_at": time.time(),
                    })

                # Mark as done
                _discovery_progress[task_key].update({
                    "status": "done",
                    "current_brand": "",
                    "message": f"Found {total_products} products ({total_new} new)",
                    "updated_at": time.time(),
                })

                logger.info(
                    f"Background retailer discovery for {retailer.name}: "
                    f"{total_new} new products"
                )
            finally:
                await scraper.close()
                for s in all_scrapers.values():
                    if s is not scraper:
                        await s.close()

    except Exception as exc:
        logger.exception(f"Background discovery failed for retailer_id={retailer_id}")
        _discovery_progress[task_key] = {
            "status": "error",
            "current_brand": "",
            "brands_done": 0,
            "brands_total": 0,
            "products_found": 0,
            "new_products": 0,
            "message": f"Error: {str(exc)[:200]}",
            "updated_at": time.time(),
        }


@router.post("/discover")
async def discover_all(request: Request):
    """Trigger full product discovery for all brands."""
    _spawn(_discover_all_background())
    return RedirectResponse("/?success=discovery_started", status_code=HTTP_303_SEE_OTHER)


async def _check_all_prices_background() -> None:
    """Run one price-check batch (and dead-product cleanup) in the background."""
    from src.tracking.price_checker import check_all_prices

    scrapers = _get_working_scrapers()
    try:
        async with async_session() as session:
            stats = await check_all_prices(session, scrapers)
            logger.info(
                f"Manual price check batch complete: {stats['checked']} updated, "
                f"{stats['removed']} removed, {stats['failed']} failed; "
                f"{stats['remaining']} still stale"
            )
    except Exception:
        logger.exception("Manual price check failed")
    finally:
        for s in scrapers.values():
            await s.close()


@router.post("/price-check")
async def price_check_all(request: Request):
    """Run one price-check batch immediately instead of waiting for the timer.

    Deliberately one batch, not the whole catalogue: an unbounded sweep is what
    used to exhaust the instance's memory and die partway through. This just
    advances the same rolling queue the scheduler works through.
    """
    _spawn(_check_all_prices_background())
    return RedirectResponse("/?success=discovery_started", status_code=HTTP_303_SEE_OTHER)


@router.post("/brands/{brand_id}/discover")
async def discover_brand(request: Request, brand_id: int):
    """Trigger product discovery for a single brand."""
    _spawn(_discover_brand_background(brand_id))
    return RedirectResponse(
        f"/brands/{brand_id}?success=discovery_started",
        status_code=HTTP_303_SEE_OTHER,
    )


@router.get("/search")
async def search_products(
    request: Request,
    q: str = "",
    page: int = 1,
    per_page: int = 40,
    session: AsyncSession = Depends(get_session),
):
    """Global product search across all brands and retailers."""
    products = []
    total_products = 0
    total_pages = 1

    if q.strip():
        search_term = q.strip()
        query = (
            select(Product)
            .join(Brand, Product.brand_id == Brand.id)
            .options(selectinload(Product.brand), selectinload(Product.retailer))
            .where(
                Product.name.ilike(f"%{search_term}%")
                | Brand.name.ilike(f"%{search_term}%")
            )
            .order_by(Product.current_price.asc().nullslast())
        )

        count_q = select(func.count()).select_from(query.subquery())
        total_products = (await session.execute(count_q)).scalar() or 0

        total_pages = max(1, (total_products + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))

        products_result = await session.execute(
            query.offset((page - 1) * per_page).limit(per_page)
        )
        products = products_result.scalars().all()

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "search_results.html",
        {
            "products": products,
            "query": q.strip(),
            "total_products": total_products,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "current_q": q.strip(),
            "current_gender": "",
            "current_sort": "",
            "unread_count": unread_count,
            "format_price": format_price,
            "is_admin": _is_admin(request),
        },
    )


@router.get("/brands/{brand_id}")
async def brand_detail(
    request: Request,
    brand_id: int,
    session: AsyncSession = Depends(get_session),
    success: str = "",
    error: str = "",
    page: int = 1,
    per_page: int = 40,
    q: str = "",
    gender: str = "",
    sort: str = "price-asc",
):
    brand = await session.get(Brand, brand_id)
    if not brand:
        return RedirectResponse("/?error=brand_not_found", status_code=HTTP_303_SEE_OTHER)

    # Build dynamic query with filters
    query = (
        select(Product)
        .where(Product.brand_id == brand_id)
        .options(selectinload(Product.retailer))
    )

    if q.strip():
        query = query.where(Product.name.ilike(f"%{q.strip()}%"))

    if gender and gender != "all":
        query = query.where(Product.gender == gender)

    # Sort
    if sort == "price-desc":
        query = query.order_by(Product.current_price.desc().nullslast())
    elif sort == "name-asc":
        query = query.order_by(Product.name.asc())
    elif sort == "name-desc":
        query = query.order_by(Product.name.desc())
    else:  # default: price-asc
        query = query.order_by(Product.current_price.asc().nullslast())

    # Count total for pagination
    count_q = select(func.count()).select_from(query.subquery())
    total_products = (await session.execute(count_q)).scalar() or 0

    total_pages = max(1, (total_products + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    products_result = await session.execute(query.offset(offset).limit(per_page))
    products = products_result.scalars().all()

    # Check if any products for this brand have gender data (for showing filter)
    gender_check = await session.execute(
        select(func.count()).where(
            Product.brand_id == brand_id,
            Product.gender.isnot(None),
            Product.gender != "",
        )
    )
    has_gender_data = (gender_check.scalar() or 0) > 0

    # Compute cheapest product IDs for "Best Price" badges
    from src.tracking.comparison import compute_cheapest_ids
    cheapest_ids = compute_cheapest_ids(products, brand.name)

    # Linked retailers with stats
    retailer_stats_q = (
        select(
            Retailer.id,
            Retailer.name,
            Retailer.slug,
            Retailer.base_url,
            Retailer.active,
            BrandRetailer.brand_url,
            BrandRetailer.verified,
            func.count(Product.id).label("product_count"),
            func.max(Product.last_checked).label("last_checked"),
        )
        .join(BrandRetailer, BrandRetailer.retailer_id == Retailer.id)
        .outerjoin(
            Product,
            (Product.retailer_id == Retailer.id) & (Product.brand_id == brand_id),
        )
        .where(BrandRetailer.brand_id == brand_id)
        .group_by(
            Retailer.id,
            Retailer.name,
            Retailer.slug,
            Retailer.base_url,
            Retailer.active,
            BrandRetailer.brand_url,
            BrandRetailer.verified,
        )
        .order_by(Retailer.name)
    )
    retailer_stats_result = await session.execute(retailer_stats_q)
    linked_retailers = retailer_stats_result.all()

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    brand_success_messages = {
        "discovery_started": "Product discovery started in the background. Refresh in a few minutes to see results.",
        "brand_updated": "Brand updated successfully.",
        "brand_updated_rematching": "Brand updated. Products are being re-discovered with new aliases. This may take a few minutes. Refresh to see results.",
    }
    brand_error_messages = {
        "brand_empty_name": "Brand name cannot be empty.",
        "brand_duplicate": "A brand with this name already exists.",
        "brand_slug_taken": "A brand with a similar name already exists.",
    }

    # Parse aliases safely
    try:
        aliases = json.loads(brand.aliases) if brand.aliases and brand.aliases != "" else []
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Invalid aliases JSON for brand {brand.id}: {brand.aliases!r}")
        aliases = []

    return templates.TemplateResponse(
        request,
        "brand_detail.html",
        {
            "brand": brand,
            "products": products,
            "linked_retailers": linked_retailers,
            "aliases": aliases,
            "unread_count": unread_count,
            "format_price": format_price,
            "success_message": brand_success_messages.get(success, ""),
            "error_message": brand_error_messages.get(error, ""),
            "is_admin": _is_admin(request),
            "page": page,
            "total_pages": total_pages,
            "total_products": total_products,
            "per_page": per_page,
            "current_q": q,
            "current_gender": gender,
            "current_sort": sort,
            "has_gender_data": has_gender_data,
            "cheapest_ids": cheapest_ids,
        },
    )


@router.get("/products/{product_id}")
async def product_detail(
    request: Request, product_id: int, session: AsyncSession = Depends(get_session)
):
    product = await session.get(
        Product,
        product_id,
        options=[selectinload(Product.brand), selectinload(Product.retailer)],
    )
    if not product:
        return RedirectResponse("/?error=product_not_found", status_code=HTTP_303_SEE_OTHER)

    from src.tracking.history import get_price_trend
    from src.tracking.comparison import find_similar_products

    trend = await get_price_trend(session, product_id)
    similar_products = await find_similar_products(session, product)

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "product_detail.html",
        {
            "product": product,
            "trend": trend,
            "similar_products": similar_products,
            "unread_count": unread_count,
            "format_price": format_price,
            "is_admin": _is_admin(request),
        },
    )


@router.get("/wishlist")
async def wishlist_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    """Wishlist page — products are loaded client-side via HTMX."""
    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "wishlist.html",
        {
            "unread_count": unread_count,
            "format_price": format_price,
            "is_admin": _is_admin(request),
        },
    )


@router.get("/wishlist/products")
async def wishlist_products_partial(
    request: Request,
    ids: str = "",
    session: AsyncSession = Depends(get_session),
):
    """Return HTML partial of product cards for given product IDs."""
    id_list = []
    for x in ids.split(","):
        x = x.strip()
        if x.isdigit():
            id_list.append(int(x))
    id_list = id_list[:200]  # safety limit

    if not id_list:
        return templates.TemplateResponse(
            request,
            "components/wishlist_empty.html",
            {},
        )

    result = await session.execute(
        select(Product)
        .where(Product.id.in_(id_list))
        .options(selectinload(Product.brand), selectinload(Product.retailer))
        .order_by(Product.current_price.asc().nullslast())
    )
    products = result.scalars().all()

    if not products:
        return templates.TemplateResponse(
            request,
            "components/wishlist_empty.html",
            {},
        )

    return templates.TemplateResponse(
        request,
        "components/wishlist_grid.html",
        {
            "products": products,
            "format_price": format_price,
        },
    )


@router.get("/notifications")
async def notifications_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Notification)
        .options(
            selectinload(Notification.alert_event)
        )
        .order_by(Notification.created_at.desc())
        .limit(100)
    )
    notifications = result.scalars().all()

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "notifications": notifications,
            "unread_count": unread_count,
            "is_admin": _is_admin(request),
        },
    )


@router.get("/alerts")
async def alerts_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    rules_result = await session.execute(
        select(AlertRule)
        .options(selectinload(AlertRule.brand))
        .order_by(AlertRule.created_at.desc())
    )
    rules = rules_result.scalars().all()

    brands_result = await session.execute(
        select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
    )
    brands = brands_result.scalars().all()

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "rules": rules,
            "brands": brands,
            "unread_count": unread_count,
            "is_admin": _is_admin(request),
        },
    )


# --- Suggest Retailer ---


@router.get("/suggest-retailer")
async def suggest_retailer_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    success: str = "",
    error: str = "",
):
    suggestions_result = await session.execute(
        select(RetailerSuggestion)
        .order_by(RetailerSuggestion.created_at.desc())
        .limit(50)
    )
    suggestions = suggestions_result.scalars().all()

    # All existing retailers for reference
    retailers_result = await session.execute(
        select(Retailer).where(Retailer.active.is_(True)).order_by(Retailer.name)
    )
    retailers = retailers_result.scalars().all()

    # Product counts per retailer — single bulk query
    retailer_ids = [r.id for r in retailers]
    product_counts_result = await session.execute(
        select(Product.retailer_id, func.count(Product.id).label("cnt"))
        .where(Product.retailer_id.in_(retailer_ids))
        .group_by(Product.retailer_id)
    )
    retailer_product_counts: dict[int, int] = {row.retailer_id: row.cnt for row in product_counts_result}

    # Sort retailers: green (working) first, yellow (pending) second, red (skipped) last
    # Within each group, sort alphabetically
    def _retailer_sort_key(r: Retailer) -> tuple:
        is_skipped = r.scraper_type in SKIP_SCRAPERS
        product_count = retailer_product_counts.get(r.id, 0)
        if is_skipped:
            group = 2  # red — last
        elif product_count > 0:
            group = 0  # green — first
        else:
            group = 1  # yellow — middle
        return (group, r.name.lower())

    retailers = sorted(retailers, key=_retailer_sort_key)

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    # Map error codes to messages
    error_messages = {
        "invalid_url": "Invalid URL — must include https://",
        "duplicate": "A retailer with this URL already exists.",
        "slug_taken": "A retailer with this name already exists.",
        "retailer_not_found": "Retailer not found.",
        "retailer_empty_name": "Retailer name cannot be empty.",
    }

    success_messages = {
        "1": "Retailer added successfully! Product discovery is running in the background — refresh brand pages in a few minutes to see results.",
        "discovery_started": "Re-discovery started for this retailer. Refresh in a few minutes to see results.",
        "retailer_updated": "Retailer updated successfully.",
        "retailer_deleted": "Retailer deleted.",
    }

    return templates.TemplateResponse(
        request,
        "suggest_retailer.html",
        {
            "suggestions": suggestions,
            "retailers": retailers,
            "retailer_product_counts": retailer_product_counts,
            "skip_scrapers": SKIP_SCRAPERS,
            "unread_count": unread_count,
            "success": bool(success),
            "success_message": success_messages.get(success, "") if success else "",
            "error_message": error_messages.get(error, ""),
            "is_admin": _is_admin(request),
        },
    )


@router.post("/retailers/{retailer_id}/discover")
async def discover_retailer(request: Request, retailer_id: int):
    """Trigger product discovery for a single retailer."""
    task_key = f"retailer-{retailer_id}"

    # Prevent duplicate discovery runs
    existing = _discovery_progress.get(task_key)
    if existing and existing.get("status") == "running":
        return JSONResponse(
            {"status": "already_running", "task_key": task_key},
            status_code=409,
        )

    # Clean up stale entries
    _cleanup_stale_progress()

    _spawn(_discover_retailer_background(retailer_id))
    return JSONResponse({"status": "started", "task_key": task_key})


@router.get("/retailers/{retailer_id}/discover-progress")
async def discover_progress_sse(request: Request, retailer_id: int):
    """SSE endpoint that streams discovery progress events."""
    task_key = f"retailer-{retailer_id}"

    async def event_generator():
        """Yield SSE events until discovery completes or client disconnects."""
        while True:
            if await request.is_disconnected():
                break

            progress = _discovery_progress.get(task_key)

            if progress is None:
                yield f"data: {json.dumps({'status': 'idle'})}\n\n"
                break

            yield f"data: {json.dumps(progress)}\n\n"

            if progress["status"] in ("done", "error"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/retailers/{retailer_id}/edit")
async def edit_retailer(
    request: Request,
    retailer_id: int,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Edit a retailer's name."""
    retailer = await session.get(Retailer, retailer_id)
    if not retailer:
        return RedirectResponse(
            "/suggest-retailer?error=retailer_not_found",
            status_code=HTTP_303_SEE_OTHER,
        )

    name = name.strip()
    if not name:
        return RedirectResponse(
            "/suggest-retailer?error=retailer_empty_name",
            status_code=HTTP_303_SEE_OTHER,
        )

    # Check name uniqueness (excluding self)
    if name != retailer.name:
        existing = await session.execute(
            select(Retailer).where(Retailer.name == name, Retailer.id != retailer_id)
        )
        if existing.scalar_one_or_none():
            return RedirectResponse(
                "/suggest-retailer?error=slug_taken",
                status_code=HTTP_303_SEE_OTHER,
            )

        retailer.name = name
        retailer.slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    await session.commit()
    logger.info(f"Retailer updated: {retailer.name} (id={retailer_id})")

    return RedirectResponse(
        "/suggest-retailer?success=retailer_updated",
        status_code=HTTP_303_SEE_OTHER,
    )


@router.post("/retailers/{retailer_id}/delete")
async def delete_retailer(
    request: Request,
    retailer_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a retailer and all its associated products."""
    retailer = await session.get(Retailer, retailer_id)
    if not retailer:
        return RedirectResponse(
            "/suggest-retailer?error=retailer_not_found",
            status_code=HTTP_303_SEE_OTHER,
        )

    retailer_name = retailer.name
    await session.delete(retailer)
    await session.commit()
    logger.info(f"Retailer deleted: {retailer_name} (id={retailer_id})")

    return RedirectResponse(
        "/suggest-retailer?success=retailer_deleted",
        status_code=HTTP_303_SEE_OTHER,
    )


@router.post("/suggest-retailer")
async def suggest_retailer_submit(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    import logging
    logger = logging.getLogger(__name__)

    # Validate URL
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return RedirectResponse(
            "/suggest-retailer?error=invalid_url", status_code=HTTP_303_SEE_OTHER
        )

    # Normalize URL
    url = url.rstrip("/")
    if url.startswith("http://"):
        url = "https://" + url[7:]

    # Check duplicate by base_url
    existing = await session.execute(
        select(Retailer).where(Retailer.base_url == url)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(
            "/suggest-retailer?error=duplicate", status_code=HTTP_303_SEE_OTHER
        )

    # Generate slug
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    # Check slug uniqueness
    existing_slug = await session.execute(
        select(Retailer).where(Retailer.slug == slug)
    )
    if existing_slug.scalar_one_or_none():
        return RedirectResponse(
            "/suggest-retailer?error=slug_taken", status_code=HTTP_303_SEE_OTHER
        )

    # Create suggestion record
    suggestion = RetailerSuggestion(name=name, url=url)
    session.add(suggestion)
    await session.flush()

    # Run health check
    health_ok = False
    health_msg = ""
    try:
        from src.retailers.generic import GenericScraper

        scraper = GenericScraper()
        scraper.base_url = url
        health_ok = await scraper.health_check()
        health_msg = "URL reachable" if health_ok else "URL returned non-200 status"
        await scraper.close()
    except Exception as exc:
        health_msg = f"Health check failed: {str(exc)[:200]}"

    suggestion.health_check_ok = health_ok
    suggestion.health_check_message = health_msg

    # Auto-approve if health check passes
    if health_ok:
        retailer = Retailer(
            name=name,
            slug=slug,
            base_url=url,
            scraper_type="generic",
            requires_js=False,
        )
        session.add(retailer)
        await session.flush()
        suggestion.status = "approved"
        suggestion.retailer_id = retailer.id
        logger.info(f"Retailer suggestion approved: {name} ({url})")
    else:
        suggestion.status = "failed"
        logger.warning(f"Retailer suggestion failed health check: {name} ({url})")

    await session.commit()

    # Trigger background discovery for the new retailer
    if health_ok:
        _spawn(_discover_retailer_background(retailer.id))

    return RedirectResponse(
        "/suggest-retailer?success=1", status_code=HTTP_303_SEE_OTHER
    )
