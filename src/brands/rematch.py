"""Product re-matching when brand aliases change."""
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, Product

logger = logging.getLogger(__name__)


async def rematch_brand_products(
    session: AsyncSession,
    brand: Brand,
) -> dict[str, int]:
    """Re-match products for a brand after alias changes.

    Deletes all existing products for the brand and triggers re-discovery
    with the new aliases. This ensures all products match current aliases.

    Args:
        session: Database session
        brand: Brand to re-match (with updated aliases)

    Returns:
        Stats dict: {"deleted": count}
    """
    # Count products before deletion
    count_result = await session.execute(
        select(func.count(Product.id)).where(Product.brand_id == brand.id)
    )
    product_count = count_result.scalar() or 0

    if product_count == 0:
        logger.info(f"No products to rematch for brand {brand.name}")
        return {"deleted": 0}

    logger.info(f"Deleting {product_count} products from {brand.name} for re-matching")

    # Delete all products (cascade deletes price_records, alert_events, etc.)
    await session.execute(delete(Product).where(Product.brand_id == brand.id))
    await session.commit()

    logger.info(f"Deleted {product_count} products from {brand.name}")

    return {"deleted": product_count}


async def trigger_rediscovery(brand_id: int):
    """Trigger background re-discovery for a brand after alias change.

    This runs in a background task after products are deleted.
    """
    from src.brands.discovery import discover_single_brand
    from src.db.models import Brand
    from src.db.session import async_session
    from src.retailers import get_all_scrapers

    try:
        async with async_session() as session:
            brand = await session.get(Brand, brand_id)
            if not brand:
                logger.error(f"Brand {brand_id} not found for re-discovery")
                return

            scrapers = get_all_scrapers()
            stats = await discover_single_brand(session, brand, scrapers)

            logger.info(
                f"Re-discovery complete for {brand.name}: "
                f"{stats.get('new_products', 0)} products found"
            )
    except Exception as e:
        logger.exception(f"Re-discovery failed for brand {brand_id}: {e}")
