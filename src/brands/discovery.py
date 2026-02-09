from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, PriceRecord, Product, Retailer
from src.retailers.base import RetailerBase, ScrapedProduct

logger = logging.getLogger(__name__)


async def discover_brand_at_retailer(
    session: AsyncSession,
    brand: Brand,
    retailer: Retailer,
    scraper: RetailerBase,
) -> list[ScrapedProduct]:
    """Search for a brand at a retailer and return scraped products."""
    aliases = json.loads(brand.aliases) if brand.aliases else []
    search_terms = [brand.name] + aliases

    for term in search_terms:
        try:
            products = await scraper.search_brand(term)
            if products:
                # Create or verify BrandRetailer mapping
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
                    await session.flush()
                    logger.info(
                        f"Discovered: {brand.name} at {retailer.name} "
                        f"({len(products)} products)"
                    )
                return products
        except Exception:
            logger.exception(
                f"Discovery failed: {brand.name} at {retailer.name} "
                f"(term: {term})"
            )

    return []


async def store_scraped_products(
    session: AsyncSession,
    brand: Brand,
    retailer: Retailer,
    scraped: list[ScrapedProduct],
) -> int:
    """Store scraped products in the DB. Returns count of new products."""
    now = dt.datetime.utcnow()
    new_count = 0

    for sp in scraped:
        if not sp.url:
            continue

        # Check if product already exists (by URL)
        existing = await session.execute(
            select(Product).where(Product.url == sp.url)
        )
        product = existing.scalar_one_or_none()

        if product:
            # Update existing product
            product.current_price = sp.price
            product.original_price = sp.original_price
            product.on_sale = sp.on_sale
            product.last_checked = now
            if sp.image_url:
                product.image_url = sp.image_url
            if sp.thumbnail_url:
                product.thumbnail_url = sp.thumbnail_url
            if sp.gender:
                product.gender = sp.gender
        else:
            # Create new product
            product = Product(
                name=sp.name,
                brand_id=brand.id,
                retailer_id=retailer.id,
                url=sp.url,
                image_url=sp.image_url or "",
                thumbnail_url=sp.thumbnail_url or "",
                sku=sp.sku or "",
                gender=sp.gender or "",
                current_price=sp.price,
                original_price=sp.original_price,
                on_sale=sp.on_sale,
                tracked=True,
                last_checked=now,
            )
            session.add(product)
            new_count += 1

        await session.flush()

        # Create initial price record
        record = PriceRecord(
            product_id=product.id,
            price=sp.price,
            original_price=sp.original_price,
            on_sale=sp.on_sale,
            currency="CAD",
        )
        session.add(record)

    await session.commit()
    return new_count


async def discover_and_store(
    session: AsyncSession,
    scrapers: dict[str, RetailerBase],
) -> dict[str, int]:
    """Run full discovery: search all brands across all retailers,
    store products and price records. Returns stats dict."""
    brands_result = await session.execute(
        select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
    )
    brands = list(brands_result.scalars().all())

    retailers_result = await session.execute(
        select(Retailer).where(Retailer.active.is_(True)).order_by(Retailer.name)
    )
    retailers = list(retailers_result.scalars().all())

    stats = {
        "brands_checked": len(brands),
        "retailers_checked": len(retailers),
        "mappings_created": 0,
        "products_found": 0,
        "new_products": 0,
    }

    for brand in brands:
        for retailer in retailers:
            scraper = scrapers.get(retailer.scraper_type)
            if scraper is None:
                continue

            logger.info(f"Searching {retailer.name} for {brand.name}...")
            scraped = await discover_brand_at_retailer(
                session, brand, retailer, scraper
            )

            if scraped:
                stats["products_found"] += len(scraped)
                new = await store_scraped_products(
                    session, brand, retailer, scraped
                )
                stats["new_products"] += new
                if new > 0:
                    stats["mappings_created"] += 1

    logger.info(
        f"Discovery complete: {stats['new_products']} new products, "
        f"{stats['mappings_created']} new mappings"
    )
    return stats
