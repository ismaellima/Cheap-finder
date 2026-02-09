from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import PriceRecord, Product


@dataclass
class PriceTrend:
    product_id: int
    product_name: str
    current_price: int
    lowest_price: int
    highest_price: int
    average_price: float
    price_history: list[dict]
    total_records: int


async def get_price_history(
    session: AsyncSession,
    product_id: int,
    days: int = 90,
) -> list[PriceRecord]:
    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    result = await session.execute(
        select(PriceRecord)
        .where(PriceRecord.product_id == product_id)
        .where(PriceRecord.recorded_at >= since)
        .order_by(PriceRecord.recorded_at.asc())
    )
    return list(result.scalars().all())


async def get_price_trend(
    session: AsyncSession,
    product_id: int,
    days: int = 90,
) -> PriceTrend | None:
    product = await session.get(Product, product_id)
    if not product:
        return None

    records = await get_price_history(session, product_id, days)
    if not records:
        return PriceTrend(
            product_id=product_id,
            product_name=product.name,
            current_price=product.current_price or 0,
            lowest_price=product.current_price or 0,
            highest_price=product.current_price or 0,
            average_price=float(product.current_price or 0),
            price_history=[],
            total_records=0,
        )

    prices = [r.price for r in records]
    history = [
        {
            "date": r.recorded_at.isoformat(),
            "price": r.price,
            "on_sale": r.on_sale,
        }
        for r in records
    ]

    return PriceTrend(
        product_id=product_id,
        product_name=product.name,
        current_price=product.current_price or prices[-1],
        lowest_price=min(prices),
        highest_price=max(prices),
        average_price=sum(prices) / len(prices),
        price_history=history,
        total_records=len(records),
    )


async def get_best_price_across_retailers(
    session: AsyncSession,
    brand_id: int,
    product_name_like: str = "",
) -> list[Product]:
    query = (
        select(Product)
        .where(Product.brand_id == brand_id)
        .where(Product.tracked.is_(True))
        .where(Product.current_price.isnot(None))
        .order_by(Product.current_price.asc())
    )
    if product_name_like:
        query = query.where(Product.name.ilike(f"%{product_name_like}%"))

    result = await session.execute(query)
    return list(result.scalars().all())
