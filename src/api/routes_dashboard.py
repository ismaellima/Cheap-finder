from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
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

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="src/templates")

# Make auth_enabled available in all templates (for logout button in nav)
templates.env.globals["auth_enabled"] = bool(settings.DASHBOARD_PASSWORD)


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
    """Run discovery for all brands at a single retailer in the background."""
    from src.brands.discovery import discover_single_retailer
    from src.retailers import get_all_scrapers

    try:
        async with async_session() as session:
            retailer = await session.get(Retailer, retailer_id)
            if not retailer:
                return
            all_scrapers = get_all_scrapers()
            scraper = all_scrapers.get(retailer.scraper_type)
            if not scraper:
                return
            try:
                stats = await discover_single_retailer(session, retailer, scraper)
                logger.info(
                    f"Background retailer discovery for {retailer.name}: "
                    f"{stats['new_products']} new products"
                )
            finally:
                await scraper.close()
                # Close any other scrapers that were instantiated
                for s in all_scrapers.values():
                    if s is not scraper:
                        await s.close()
    except Exception:
        logger.exception(f"Background discovery failed for retailer_id={retailer_id}")


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


@router.get("/brands/{brand_id}")
async def brand_detail(
    request: Request,
    brand_id: int,
    session: AsyncSession = Depends(get_session),
    success: str = "",
):
    brand = await session.get(Brand, brand_id)
    if not brand:
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "error": "Brand not found"},
            status_code=404,
        )

    products_result = await session.execute(
        select(Product)
        .where(Product.brand_id == brand_id)
        .options(selectinload(Product.retailer))
        .order_by(Product.current_price.asc().nullslast())
    )
    products = products_result.scalars().all()

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
            {"request": request, "error": "Product not found"},
            status_code=404,
        )

    from src.tracking.history import get_price_trend

    trend = await get_price_trend(session, product_id)

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
            "unread_count": unread_count,
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

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    # Map error codes to messages
    error_messages = {
        "invalid_url": "Invalid URL — must include https://",
        "duplicate": "A retailer with this URL already exists.",
        "slug_taken": "A retailer with this name already exists.",
    }

    return templates.TemplateResponse(
        "suggest_retailer.html",
        {
            "request": request,
            "suggestions": suggestions,
            "retailers": retailers,
            "unread_count": unread_count,
            "success": bool(success),
            "error_message": error_messages.get(error, ""),
        },
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
