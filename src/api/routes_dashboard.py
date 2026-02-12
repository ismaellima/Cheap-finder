from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
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
    Product,
    Retailer,
    RetailerSuggestion,
)
from src.db.session import async_session, get_session

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
    }

    brands_result = await session.execute(
        select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
    )
    brands = brands_result.scalars().all()

    # Recent price drops
    drops_result = await session.execute(
        select(Product)
        .where(Product.on_sale.is_(True))
        .options(selectinload(Product.brand), selectinload(Product.retailer))
        .order_by(Product.last_checked.desc().nullslast())
        .limit(12)
    )
    recent_drops = drops_result.scalars().all()

    # Unread notification count
    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    # Brand stats + retailer names
    brand_stats = {}
    brand_retailers_map = {}
    for brand in brands:
        count_result = await session.execute(
            select(func.count(Product.id)).where(Product.brand_id == brand.id)
        )
        retailer_count = await session.execute(
            select(func.count(BrandRetailer.id)).where(
                BrandRetailer.brand_id == brand.id
            )
        )
        brand_stats[brand.id] = {
            "product_count": count_result.scalar() or 0,
            "retailer_count": retailer_count.scalar() or 0,
        }

        # Fetch retailer names for badges
        retailers_result = await session.execute(
            select(Retailer.name)
            .join(BrandRetailer, BrandRetailer.retailer_id == Retailer.id)
            .where(BrandRetailer.brand_id == brand.id)
            .order_by(Retailer.name)
        )
        brand_retailers_map[brand.id] = [r[0] for r in retailers_result.all()]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "brands": brands,
            "brand_stats": brand_stats,
            "brand_retailers_map": brand_retailers_map,
            "recent_drops": recent_drops,
            "unread_count": unread_count,
            "format_price": format_price,
            "success_message": success_messages.get(success, ""),
            "error_message": error_messages.get(error, ""),
            "is_admin": _is_admin(request),
        },
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
    asyncio.create_task(_discover_brand_background(brand_id))

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
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    brand.aliases = json.dumps(alias_list)

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
    logger.info(f"Brand updated: {brand.name} (id={brand_id})")

    return RedirectResponse(
        f"/brands/{brand_id}?success=brand_updated",
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
    asyncio.create_task(_discover_all_background())
    return RedirectResponse("/?success=discovery_started", status_code=HTTP_303_SEE_OTHER)


@router.post("/brands/{brand_id}/discover")
async def discover_brand(request: Request, brand_id: int):
    """Trigger product discovery for a single brand."""
    asyncio.create_task(_discover_brand_background(brand_id))
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
        "search_results.html",
        {
            "request": request,
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
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "error": "Brand not found", "is_admin": _is_admin(request)},
            status_code=404,
        )

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
    }
    brand_error_messages = {
        "brand_empty_name": "Brand name cannot be empty.",
        "brand_duplicate": "A brand with this name already exists.",
        "brand_slug_taken": "A brand with a similar name already exists.",
    }

    return templates.TemplateResponse(
        "brand_detail.html",
        {
            "request": request,
            "brand": brand,
            "products": products,
            "linked_retailers": linked_retailers,
            "aliases": json.loads(brand.aliases) if brand.aliases else [],
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
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "error": "Product not found", "is_admin": _is_admin(request)},
            status_code=404,
        )

    from src.tracking.history import get_price_trend
    from src.tracking.comparison import find_similar_products

    trend = await get_price_trend(session, product_id)
    similar_products = await find_similar_products(session, product)

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        "product_detail.html",
        {
            "request": request,
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
        "wishlist.html",
        {
            "request": request,
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
            "components/wishlist_empty.html",
            {"request": request},
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
            "components/wishlist_empty.html",
            {"request": request},
        )

    return templates.TemplateResponse(
        "components/wishlist_grid.html",
        {
            "request": request,
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
        "notifications.html",
        {
            "request": request,
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
        "alerts.html",
        {
            "request": request,
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

    # Product counts per retailer
    retailer_product_counts: dict[int, int] = {}
    for r in retailers:
        count_result = await session.execute(
            select(func.count(Product.id)).where(Product.retailer_id == r.id)
        )
        retailer_product_counts[r.id] = count_result.scalar() or 0

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
        "suggest_retailer.html",
        {
            "request": request,
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

    asyncio.create_task(_discover_retailer_background(retailer_id))
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
        asyncio.create_task(_discover_retailer_background(retailer.id))

    return RedirectResponse(
        "/suggest-retailer?success=1", status_code=HTTP_303_SEE_OTHER
    )
