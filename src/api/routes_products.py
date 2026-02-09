from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import Product
from src.db.session import get_session
from src.tracking.history import get_price_trend

router = APIRouter(prefix="/api/products", tags=["products"])


class ProductCreate(BaseModel):
    name: str
    brand_id: int
    retailer_id: int
    url: str
    image_url: str = ""
    sku: str = ""
    gender: str = ""  # men, women, unisex


@router.get("")
async def list_products(
    brand_id: int | None = None,
    on_sale: bool | None = None,
    gender: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    query = select(Product).options(
        selectinload(Product.brand), selectinload(Product.retailer)
    )
    if brand_id is not None:
        query = query.where(Product.brand_id == brand_id)
    if on_sale is not None:
        query = query.where(Product.on_sale.is_(on_sale))
    if gender is not None:
        query = query.where(Product.gender == gender)

    query = query.order_by(Product.current_price.asc().nullslast())
    result = await session.execute(query)
    products = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "brand": p.brand.name if p.brand else None,
            "retailer": p.retailer.name if p.retailer else None,
            "url": p.url,
            "current_price": p.current_price,
            "original_price": p.original_price,
            "on_sale": p.on_sale,
            "image_url": p.image_url,
            "thumbnail_url": p.thumbnail_url,
            "gender": p.gender,
            "last_checked": p.last_checked.isoformat() if p.last_checked else None,
        }
        for p in products
    ]


@router.get("/{product_id}")
async def get_product(product_id: int, session: AsyncSession = Depends(get_session)):
    product = await session.get(
        Product,
        product_id,
        options=[selectinload(Product.brand), selectinload(Product.retailer)],
    )
    if not product:
        raise HTTPException(404, "Product not found")

    trend = await get_price_trend(session, product_id)

    return {
        "id": product.id,
        "name": product.name,
        "brand": product.brand.name if product.brand else None,
        "retailer": product.retailer.name if product.retailer else None,
        "url": product.url,
        "current_price": product.current_price,
        "original_price": product.original_price,
        "on_sale": product.on_sale,
        "image_url": product.image_url,
        "thumbnail_url": product.thumbnail_url,
        "gender": product.gender,
        "last_checked": product.last_checked.isoformat() if product.last_checked else None,
        "trend": {
            "lowest_price": trend.lowest_price,
            "highest_price": trend.highest_price,
            "average_price": round(trend.average_price),
            "price_history": trend.price_history,
            "total_records": trend.total_records,
        } if trend else None,
    }


@router.post("")
async def create_product(
    data: ProductCreate, session: AsyncSession = Depends(get_session)
):
    product = Product(
        name=data.name,
        brand_id=data.brand_id,
        retailer_id=data.retailer_id,
        url=data.url,
        image_url=data.image_url,
        sku=data.sku,
        gender=data.gender,
    )
    session.add(product)
    await session.commit()
    return {"id": product.id, "name": product.name}


@router.delete("/{product_id}")
async def delete_product(
    product_id: int, session: AsyncSession = Depends(get_session)
):
    product = await session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    await session.delete(product)
    await session.commit()
    return {"deleted": True}
