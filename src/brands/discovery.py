from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, Retailer
from src.retailers.base import RetailerBase

logger = logging.getLogger(__name__)


async def discover_brand_at_retailer(
    session: AsyncSession,
    brand: Brand,
    retailer: Retailer,
    scraper: RetailerBase,
) -> bool:
    aliases = json.loads(brand.aliases) if brand.aliases else []
    search_terms = [brand.name] + aliases

    for term in search_terms:
        try:
            products = await scraper.search_brand(term)
            if products:
                existing = await session.execute(
                    select(BrandRetailer).where(
                        BrandRetailer.brand_id == brand.id,
                        BrandRetailer.retailer_id == retailer.id,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    br = BrandRetailer(
                        brand_id=brand.id,
                        retailer_id=retailer.id,
                        verified=True,
                    )
                    session.add(br)
                    await session.commit()
                    logger.info(
                        f"Discovered: {brand.name} at {retailer.name} "
                        f"({len(products)} products)"
                    )
                return True
        except Exception:
            logger.exception(
                f"Discovery failed: {brand.name} at {retailer.name} "
                f"(term: {term})"
            )

    return False


async def discover_brand_across_retailers(
    session: AsyncSession,
    brand: Brand,
    scrapers: dict[str, RetailerBase],
) -> list[Retailer]:
    result = await session.execute(select(Retailer).where(Retailer.active.is_(True)))
    retailers = list(result.scalars().all())

    found_at: list[Retailer] = []
    for retailer in retailers:
        scraper = scrapers.get(retailer.scraper_type)
        if scraper is None:
            continue

        found = await discover_brand_at_retailer(session, brand, retailer, scraper)
        if found:
            found_at.append(retailer)

    logger.info(
        f"Discovery complete for {brand.name}: "
        f"found at {len(found_at)}/{len(retailers)} retailers"
    )
    return found_at
