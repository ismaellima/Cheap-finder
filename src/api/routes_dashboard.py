from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config import settings
from src.db.models import Brand, BrandRetailer, Notification, Product
from src.db.session import get_session

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="src/templates")

# Make auth_enabled available in all templates (for logout button in nav)
templates.env.globals["auth_enabled"] = bool(settings.DASHBOARD_PASSWORD)


def format_price(cents: int | None) -> str:
    if cents is None:
        return "N/A"
    return f"${cents / 100:,.2f}"


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
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

    # Brand product counts
    brand_stats = {}
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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "brands": brands,
            "brand_stats": brand_stats,
            "recent_drops": recent_drops,
            "unread_count": unread_count,
            "format_price": format_price,
        },
    )


@router.get("/brands/{brand_id}")
async def brand_detail(
    request: Request, brand_id: int, session: AsyncSession = Depends(get_session)
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

    unread_result = await session.execute(
        select(func.count(Notification.id)).where(Notification.read.is_(False))
    )
    unread_count = unread_result.scalar() or 0

    return templates.TemplateResponse(
        "brand_detail.html",
        {
            "request": request,
            "brand": brand,
            "products": products,
            "aliases": json.loads(brand.aliases) if brand.aliases else [],
            "unread_count": unread_count,
            "format_price": format_price,
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
    from src.db.models import AlertRule

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
