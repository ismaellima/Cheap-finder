from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.alerts.notifier import send_alert
from src.alerts.rules import check_price_alert
from src.db.models import AlertEvent, PriceRecord, Product
from src.retailers.base import RetailerBase

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    record: PriceRecord | None = None
    removed: bool = False


async def _product_page_is_gone(url: str) -> bool:
    """Confirm a product's own URL returns a definitive 404 (page removed).

    Used only as a secondary check when a scraper fails to return data, so a
    layout change or a transient network error doesn't get misread as a
    delisted product — we only trust an explicit 404 on the exact stored URL.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            return resp.status_code == 404
    except Exception:
        return False


async def _delete_removed_product(session: AsyncSession, product: Product) -> None:
    """Delete a product that's confirmed gone, along with its history.

    Price records cascade via the ORM relationship, but alert events (and
    their notifications) reference products with a NOT NULL FK and no
    cascade, so they have to be removed explicitly first.
    """
    events_result = await session.execute(
        select(AlertEvent)
        .where(AlertEvent.product_id == product.id)
        .options(selectinload(AlertEvent.notifications))
    )
    for event in events_result.scalars().all():
        await session.delete(event)

    await session.delete(product)
    await session.commit()


async def check_product_price(
    session: AsyncSession,
    product: Product,
    scraper: RetailerBase,
) -> CheckResult:
    try:
        result = await scraper.get_price(product.url)
    except Exception:
        logger.exception(f"Failed to scrape price for {product.name} ({product.url})")
        result = None

    if result is None:
        if await _product_page_is_gone(product.url):
            logger.warning(f"Product removed (404), deleting: {product.name} ({product.url})")
            await _delete_removed_product(session, product)
            return CheckResult(removed=True)
        logger.warning(f"Could not get price for {product.name} ({product.url})")
        return CheckResult()

    if not result.available:
        logger.info(f"Product out of stock, keeping: {product.name}")
        return CheckResult()

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
    return CheckResult(record=record)


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
    removed = 0
    for product in products:
        scraper_type = product.retailer.scraper_type if product.retailer else "generic"
        scraper = scrapers.get(scraper_type)
        if scraper is None:
            logger.warning(f"No scraper for {scraper_type}, skipping {product.name}")
            continue

        outcome = await check_product_price(session, product, scraper)
        if outcome.removed:
            removed += 1
        elif outcome.record:
            checked += 1

    logger.info(
        f"Price check complete: {checked}/{len(products)} products updated, "
        f"{removed} removed (no longer available)"
    )
    return checked
