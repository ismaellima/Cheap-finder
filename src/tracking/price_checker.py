from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.alerts.notifier import send_alert
from src.alerts.rules import check_price_alert
from src.db.models import PriceRecord, Product
from src.retailers.base import RetailerBase

logger = logging.getLogger(__name__)


async def check_product_price(
    session: AsyncSession,
    product: Product,
    scraper: RetailerBase,
) -> PriceRecord | None:
    try:
        result = await scraper.get_price(product.url)
    except Exception:
        logger.exception(f"Failed to scrape price for {product.name} ({product.url})")
        return None

    if result is None or not result.available:
        logger.warning(f"Product unavailable: {product.name}")
        return None

    old_price = product.current_price or 0

    record = PriceRecord(
        product_id=product.id,
        price=result.price,
        original_price=result.original_price,
        on_sale=result.on_sale,
        currency=result.currency,
    )
    session.add(record)

    product.current_price = result.price
    product.original_price = result.original_price
    product.on_sale = result.on_sale
    product.last_checked = dt.datetime.utcnow()

    if old_price > 0 and result.price < old_price:
        events = await check_price_alert(session, product, old_price, result.price)
        for event in events:
            await send_alert(event)

    await session.commit()
    logger.info(f"Checked {product.name}: ${result.price / 100:.2f}")
    return record


async def check_all_prices(
    session: AsyncSession,
    scrapers: dict[str, RetailerBase],
) -> int:
    result = await session.execute(
        select(Product)
        .where(Product.tracked.is_(True))
        .options(selectinload(Product.brand), selectinload(Product.retailer))
    )
    products = list(result.scalars().all())

    checked = 0
    for product in products:
        scraper_type = product.retailer.scraper_type if product.retailer else "generic"
        scraper = scrapers.get(scraper_type)
        if scraper is None:
            logger.warning(f"No scraper for {scraper_type}, skipping {product.name}")
            continue

        record = await check_product_price(session, product, scraper)
        if record:
            checked += 1

    logger.info(f"Price check complete: {checked}/{len(products)} products updated")
    return checked
