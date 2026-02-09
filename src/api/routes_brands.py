from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import AlertRule, Brand, BrandRetailer, Product, Retailer
from src.db.session import get_session

router = APIRouter(prefix="/api/brands", tags=["brands"])


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
