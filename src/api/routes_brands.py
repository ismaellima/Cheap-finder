from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import AlertRule, Brand, BrandRetailer, PriceRecord, Product, Retailer
from src.db.session import async_session, get_session

router = APIRouter(prefix="/api/brands", tags=["brands"])


# --- Export endpoint (brands + retailers in one call) ---

from fastapi import APIRouter as _AR

export_router = _AR(prefix="/api", tags=["export"])


@export_router.get("/export")
async def export_all(session: AsyncSession = Depends(get_session)):
    """Export all brands and retailers as JSON for syncing to local dev."""
    brands_result = await session.execute(select(Brand).order_by(Brand.name))
    brands = brands_result.scalars().all()

    retailers_result = await session.execute(select(Retailer).order_by(Retailer.name))
    retailers = retailers_result.scalars().all()

    return {
        "brands": [
            {
                "name": b.name,
                "slug": b.slug,
                "aliases": json.loads(b.aliases) if b.aliases else [],
                "category": b.category or "",
                "alert_threshold_pct": b.alert_threshold_pct,
                "active": b.active,
            }
            for b in brands
        ],
        "retailers": [
            {
                "name": r.name,
                "slug": r.slug,
                "base_url": r.base_url,
                "scraper_type": r.scraper_type,
                "requires_js": r.requires_js,
                "active": r.active,
            }
            for r in retailers
        ],
    }


# --- Full export (paginated) ---
#
# Prod runs on a 512 MB instance, and building every product and price record
# into one dict took it down. The export is split instead: /export-full/meta
# carries the small tables, and products and price records come in pages keyed
# on id (?after_id=&limit=), so no request holds more than one page in memory.
# /export-full still returns the whole thing, but streams it page by page.

EXPORT_PAGE_DEFAULT = 1000
EXPORT_PAGE_MAX = 5000


def _iso(value) -> str | None:
    return value.isoformat() if value else None


async def _export_meta(session: AsyncSession) -> dict:
    brands = (await session.execute(select(Brand).order_by(Brand.name))).scalars().all()
    retailers = (await session.execute(select(Retailer).order_by(Retailer.name))).scalars().all()
    br_rows = (
        await session.execute(
            select(BrandRetailer.brand_url, BrandRetailer.verified, Brand.slug, Retailer.slug)
            .join(Brand, BrandRetailer.brand_id == Brand.id)
            .join(Retailer, BrandRetailer.retailer_id == Retailer.id)
            .order_by(BrandRetailer.id)
        )
    ).all()

    return {
        "brands": [
            {
                "name": b.name,
                "slug": b.slug,
                "aliases": json.loads(b.aliases) if b.aliases else [],
                "category": b.category or "",
                "alert_threshold_pct": b.alert_threshold_pct,
                "active": b.active,
            }
            for b in brands
        ],
        "retailers": [
            {
                "name": r.name,
                "slug": r.slug,
                "base_url": r.base_url,
                "scraper_type": r.scraper_type,
                "requires_js": r.requires_js,
                "active": r.active,
            }
            for r in retailers
        ],
        "brand_retailers": [
            {
                "brand_slug": brand_slug,
                "retailer_slug": retailer_slug,
                "brand_url": brand_url or "",
                "verified": verified,
            }
            for brand_url, verified, brand_slug, retailer_slug in br_rows
        ],
        "counts": {
            "products": await session.scalar(select(func.count(Product.id))),
            "price_records": await session.scalar(select(func.count(PriceRecord.id))),
        },
    }


async def _export_products_page(
    session: AsyncSession, after_id: int, limit: int
) -> tuple[list[dict], int | None]:
    """One page of products with id > after_id. Returns (items, next_after_id)."""
    rows = (
        await session.execute(
            select(Product, Brand.slug, Retailer.slug)
            .join(Brand, Product.brand_id == Brand.id)
            .join(Retailer, Product.retailer_id == Retailer.id)
            .where(Product.id > after_id)
            .order_by(Product.id)
            .limit(limit)
        )
    ).all()

    items = [
        {
            "name": p.name,
            "brand_slug": brand_slug,
            "retailer_slug": retailer_slug,
            "url": p.url,
            "image_url": p.image_url or "",
            "thumbnail_url": p.thumbnail_url or "",
            "sku": p.sku or "",
            "gender": p.gender or "",
            "sizes": p.sizes or "",
            "current_price": p.current_price,
            "original_price": p.original_price,
            "on_sale": p.on_sale,
            "tracked": p.tracked,
            "last_checked": _iso(p.last_checked),
            "created_at": _iso(p.created_at),
        }
        for p, brand_slug, retailer_slug in rows
    ]
    next_after = rows[-1][0].id if len(rows) == limit else None
    session.expunge_all()  # don't let the identity map grow across pages
    return items, next_after


async def _export_price_records_page(
    session: AsyncSession, after_id: int, limit: int
) -> tuple[list[dict], int | None]:
    """One page of price records with id > after_id. Returns (items, next_after_id)."""
    rows = (
        await session.execute(
            select(
                PriceRecord.id,
                PriceRecord.price,
                PriceRecord.original_price,
                PriceRecord.on_sale,
                PriceRecord.currency,
                PriceRecord.recorded_at,
                Product.url,
            )
            .join(Product, PriceRecord.product_id == Product.id)
            .where(PriceRecord.id > after_id)
            .order_by(PriceRecord.id)
            .limit(limit)
        )
    ).all()

    items = [
        {
            "product_url": url,
            "price": price,
            "original_price": original_price,
            "on_sale": on_sale,
            "currency": currency,
            "recorded_at": _iso(recorded_at),
        }
        for _id, price, original_price, on_sale, currency, recorded_at, url in rows
    ]
    next_after = rows[-1][0] if len(rows) == limit else None
    return items, next_after


@export_router.get("/export-full/meta")
async def export_full_meta(session: AsyncSession = Depends(get_session)):
    """Brands (with aliases), retailers, brand-retailer mappings, and row counts."""
    return await _export_meta(session)


@export_router.get("/export-full/products")
async def export_full_products(
    after_id: int = Query(0, ge=0),
    limit: int = Query(EXPORT_PAGE_DEFAULT, ge=1, le=EXPORT_PAGE_MAX),
    session: AsyncSession = Depends(get_session),
):
    """A page of products. Pass the returned next_after_id until it is null."""
    items, next_after = await _export_products_page(session, after_id, limit)
    return {"items": items, "next_after_id": next_after}


@export_router.get("/export-full/price-records")
async def export_full_price_records(
    after_id: int = Query(0, ge=0),
    limit: int = Query(EXPORT_PAGE_DEFAULT, ge=1, le=EXPORT_PAGE_MAX),
    session: AsyncSession = Depends(get_session),
):
    """A page of price records. Pass the returned next_after_id until it is null."""
    items, next_after = await _export_price_records_page(session, after_id, limit)
    return {"items": items, "next_after_id": next_after}


async def _stream_export_full() -> AsyncIterator[bytes]:
    # The request's Depends session is closed before a streamed body is sent,
    # so the generator opens its own.
    async with async_session() as session:
        meta = await _export_meta(session)
        yield b"{"
        for key in ("brands", "retailers", "brand_retailers"):
            yield json.dumps(key).encode() + b":" + json.dumps(meta[key]).encode() + b","

        for key, fetch_page in (
            ("products", _export_products_page),
            ("price_records", _export_price_records_page),
        ):
            yield json.dumps(key).encode() + b":["
            after_id, first = 0, True
            while after_id is not None:
                items, after_id = await fetch_page(session, after_id, EXPORT_PAGE_DEFAULT)
                for item in items:
                    yield (b"" if first else b",") + json.dumps(item).encode()
                    first = False
            yield b"]" + (b"," if key == "products" else b"")
        yield b"}"


@export_router.get("/export-full")
async def export_full():
    """Export all data in one response, streamed so it never sits in memory.

    Prefer the paginated /export-full/* endpoints (scripts/sync_from_prod.py
    uses them): each page is a short request instead of one long one.
    """
    return StreamingResponse(_stream_export_full(), media_type="application/json")


class BrandCreate(BaseModel):
    name: str
    aliases: list[str] = []
    category: str = ""
    alert_threshold_pct: float = 10.0


class BrandUpdate(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    category: str | None = None
    alert_threshold_pct: float | None = None
    active: bool | None = None


@router.get("")
async def list_brands(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Brand).order_by(Brand.name)
    )
    brands = result.scalars().all()
    return [
        {
            "id": b.id,
            "name": b.name,
            "slug": b.slug,
            "aliases": json.loads(b.aliases) if b.aliases else [],
            "category": b.category,
            "alert_threshold_pct": b.alert_threshold_pct,
            "active": b.active,
        }
        for b in brands
    ]


@router.get("/{brand_id}")
async def get_brand(brand_id: int, session: AsyncSession = Depends(get_session)):
    brand = await session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")

    # Get retailers for this brand
    retailers_result = await session.execute(
        select(Retailer)
        .join(BrandRetailer)
        .where(BrandRetailer.brand_id == brand_id)
    )
    retailers = retailers_result.scalars().all()

    # Get products for this brand
    products_result = await session.execute(
        select(Product)
        .where(Product.brand_id == brand_id)
        .options(selectinload(Product.retailer))
        .order_by(Product.current_price.asc().nullslast())
    )
    products = products_result.scalars().all()

    return {
        "id": brand.id,
        "name": brand.name,
        "slug": brand.slug,
        "aliases": json.loads(brand.aliases) if brand.aliases else [],
        "category": brand.category,
        "alert_threshold_pct": brand.alert_threshold_pct,
        "active": brand.active,
        "retailers": [
            {"id": r.id, "name": r.name, "base_url": r.base_url}
            for r in retailers
        ],
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "current_price": p.current_price,
                "original_price": p.original_price,
                "on_sale": p.on_sale,
                "image_url": p.image_url,
                "thumbnail_url": p.thumbnail_url,
                "retailer": p.retailer.name if p.retailer else None,
            }
            for p in products
        ],
    }


@router.post("")
async def create_brand(data: BrandCreate, session: AsyncSession = Depends(get_session)):
    slug = data.name.lower().replace("'", "").replace(".", "").replace(" ", "-")
    brand = Brand(
        name=data.name,
        slug=slug,
        aliases=json.dumps(data.aliases),
        category=data.category,
        alert_threshold_pct=data.alert_threshold_pct,
    )
    session.add(brand)
    await session.commit()

    # Create default alert rule
    rule = AlertRule(
        brand_id=brand.id,
        condition="pct_drop",
        threshold_pct=data.alert_threshold_pct,
        notify_email=True,
        notify_dashboard=True,
    )
    session.add(rule)
    await session.commit()

    return {"id": brand.id, "name": brand.name, "slug": brand.slug}


@router.patch("/{brand_id}")
async def update_brand(
    brand_id: int, data: BrandUpdate, session: AsyncSession = Depends(get_session)
):
    brand = await session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")

    if data.name is not None:
        brand.name = data.name
    if data.aliases is not None:
        brand.aliases = json.dumps(data.aliases)
    if data.category is not None:
        brand.category = data.category
    if data.alert_threshold_pct is not None:
        brand.alert_threshold_pct = data.alert_threshold_pct
    if data.active is not None:
        brand.active = data.active

    await session.commit()
    return {"id": brand.id, "name": brand.name, "updated": True}


@router.delete("/{brand_id}")
async def delete_brand(brand_id: int, session: AsyncSession = Depends(get_session)):
    brand = await session.get(Brand, brand_id)
    if not brand:
        raise HTTPException(404, "Brand not found")
    await session.delete(brand)
    await session.commit()
    return {"deleted": True}
