from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, PriceRecord, Product, Retailer
from src.retailers.base import RetailerBase, ScrapedProduct

logger = logging.getLogger(__name__)


_KIDS_KEYWORDS = [
    "kids", "kid's", "youth", "junior", "jr.", "jr ",
    "toddler", "infant", "baby", "boy's", "boys'",
    "girl's", "girls'", "enfant", "enfants", "bébé",
    "bebe", "garçon", "garcon", "fille", "bambin",
    "little kids", "big kids", "grade school", "preschool",
    " gs", " td", " ps",  # sneaker size codes (grade school, toddler, preschool)
]


def _is_kids_product(name: str) -> bool:
    """Check if a product name indicates a kids/youth item."""
    lower = name.lower()
    return any(kw in lower for kw in _KIDS_KEYWORDS)


def _normalize(name: str) -> str:
    """Normalize a brand name for fuzzy comparison.

    Strips punctuation, extra whitespace, and lowercases.
    E.g. "Arc'teryx" → "arcteryx", "A.P.C." → "apc"
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _brand_matches(
    scraped_brand: str,
    brand_name: str,
    aliases: list[str],
) -> bool:
    """Check if a scraped product's brand matches the expected brand.

    If the scraper didn't return a brand name (empty string), we assume
    it matches (collection-scoped scrapers like Shopify already filter).

    For compound brands (containing spaces), only exact matches are allowed
    to prevent false positives like "Nike" matching "Nike ACG".

    Otherwise we compare the scraped brand against the brand name and
    all its aliases using normalized fuzzy matching.
    """
    if not scraped_brand:
        # Scraper didn't provide brand info — trust the result
        return True

    norm_scraped = _normalize(scraped_brand)
    if not norm_scraped:
        return True

    # Build set of acceptable normalized brand names
    acceptable = {_normalize(brand_name)}
    for alias in aliases:
        acceptable.add(_normalize(alias))

    # Direct match
    if norm_scraped in acceptable:
        return True

    # Substring match ONLY for single-word brands (no spaces in original)
    # This prevents "Nike" from matching "Nike ACG"
    has_compound_name = any(" " in s for s in [brand_name] + aliases)

    if not has_compound_name:
        # Allow fuzzy substring matching for single-word brands
        # Only for names >= 4 chars to avoid false positives
        # (e.g. "on" matching "salm-on", "nb" matching "bnb")
        for name in acceptable:
            if name and len(name) >= 4 and (name in norm_scraped or norm_scraped in name):
                return True

    return False


def _filter_by_brand(
    products: list[ScrapedProduct],
    brand: Brand,
) -> list[ScrapedProduct]:
    """Filter scraped products to only those matching the expected brand."""
    aliases = json.loads(brand.aliases) if brand.aliases else []
    filtered = []
    rejected = 0

    for p in products:
        if _brand_matches(p.brand, brand.name, aliases):
            filtered.append(p)
        else:
            rejected += 1

    if rejected > 0:
        logger.info(
            f"Brand filter: kept {len(filtered)}, "
            f"rejected {rejected} non-{brand.name} products"
        )

    return filtered


async def discover_brand_at_retailer(
    session: AsyncSession,
    brand: Brand,
    retailer: Retailer,
    scraper: RetailerBase,
) -> list[ScrapedProduct]:
    """Search for a brand at a retailer and return scraped products."""
    # For generic scrapers, set the base_url to the retailer's URL
    # so Shopify endpoints point to the right store
    if scraper.slug == "generic" and retailer.base_url:
        scraper.base_url = retailer.base_url.rstrip("/")

    aliases = json.loads(brand.aliases) if brand.aliases else []
    search_terms = [brand.name] + aliases

    for term in search_terms:
        try:
            products = await scraper.search_brand(term)
            if products:
                # Filter out products that don't belong to this brand
                products = _filter_by_brand(products, brand)
                if not products:
                    continue

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

        if _is_kids_product(sp.name):
            logger.debug(f"Skipping kids product: {sp.name}")
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
            if sp.sizes:
                product.sizes = sp.sizes
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
                sizes=sp.sizes or "",
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


async def discover_single_brand(
    session: AsyncSession,
    brand: Brand,
    scrapers: dict[str, RetailerBase],
) -> dict[str, int]:
    """Discover products for a single brand across all active retailers."""
    retailers_result = await session.execute(
        select(Retailer).where(Retailer.active.is_(True)).order_by(Retailer.name)
    )
    retailers = list(retailers_result.scalars().all())

    stats = {"products_found": 0, "new_products": 0, "retailers_matched": 0}

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
            stats["retailers_matched"] += 1

    logger.info(
        f"Brand discovery for {brand.name}: {stats['new_products']} products "
        f"across {stats['retailers_matched']} retailers"
    )
    return stats


async def discover_single_retailer(
    session: AsyncSession,
    retailer: Retailer,
    scraper: RetailerBase,
) -> dict[str, int]:
    """Discover products for all active brands at a single retailer."""
    brands_result = await session.execute(
        select(Brand).where(Brand.active.is_(True)).order_by(Brand.name)
    )
    brands = list(brands_result.scalars().all())

    stats = {"brands_checked": len(brands), "products_found": 0, "new_products": 0}

    for brand in brands:
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

    logger.info(
        f"Retailer discovery for {retailer.name}: {stats['new_products']} new products "
        f"across {stats['brands_checked']} brands"
    )
    return stats


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

    stats = {
        "brands_checked": len(brands),
        "retailers_checked": 0,
        "mappings_created": 0,
        "products_found": 0,
        "new_products": 0,
    }

    for brand in brands:
        brand_stats = await discover_single_brand(session, brand, scrapers)
        stats["products_found"] += brand_stats["products_found"]
        stats["new_products"] += brand_stats["new_products"]
        stats["mappings_created"] += brand_stats["retailers_matched"]

    logger.info(
        f"Discovery complete: {stats['new_products']} new products, "
        f"{stats['mappings_created']} new mappings"
    )
    return stats
