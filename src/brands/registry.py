from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, Retailer

logger = logging.getLogger(__name__)

INITIAL_BRANDS = [
    {
        "name": "On Cloud",
        "slug": "on-cloud",
        "aliases": json.dumps(["On Running", "On"]),
        "category": "running",
    },
    {
        "name": "Satisfy Running",
        "slug": "satisfy-running",
        "aliases": json.dumps(["Satisfy"]),
        "category": "running",
    },
    {
        "name": "APFR",
        "slug": "apfr",
        "aliases": json.dumps(["A.P.C.", "A.P.C", "APC"]),
        "category": "fashion",
    },
    {
        "name": "New Balance",
        "slug": "new-balance",
        "aliases": json.dumps(["NB", "New Balance Made in USA", "New Balance Made in UK"]),
        "category": "sneakers",
    },
    {
        "name": "Balmoral",
        "slug": "balmoral",
        "aliases": json.dumps([]),
        "category": "fashion",
    },
    {
        "name": "Arc'teryx",
        "slug": "arcteryx",
        "aliases": json.dumps(["Arcteryx", "Arc'teryx"]),
        "category": "outdoor",
    },
]

INITIAL_RETAILERS = [
    {
        "name": "Simons",
        "slug": "simons",
        "base_url": "https://www.simons.ca",
        "scraper_type": "simons",
        "requires_js": False,
    },
    {
        "name": "SSENSE",
        "slug": "ssense",
        "base_url": "https://www.ssense.com",
        "scraper_type": "ssense",
        "requires_js": True,
    },
    {
        "name": "Nordstrom",
        "slug": "nordstrom",
        "base_url": "https://www.nordstrom.ca",
        "scraper_type": "nordstrom",
        "requires_js": False,
    },
    {
        "name": "Sporting Life",
        "slug": "sporting-life",
        "base_url": "https://www.sportinglife.ca",
        "scraper_type": "sporting_life",
        "requires_js": False,
    },
    {
        "name": "Altitude Sports",
        "slug": "altitude-sports",
        "base_url": "https://www.altitude-sports.com",
        "scraper_type": "altitude_sports",
        "requires_js": False,
    },
    {
        "name": "Haven",
        "slug": "haven",
        "base_url": "https://havenshop.com",
        "scraper_type": "haven",
        "requires_js": False,
    },
    {
        "name": "Livestock",
        "slug": "livestock",
        "base_url": "https://www.deadstock.ca",
        "scraper_type": "livestock",
        "requires_js": False,
    },
    {
        "name": "NRML",
        "slug": "nrml",
        "base_url": "https://nrml.ca",
        "scraper_type": "nrml",
        "requires_js": False,
    },
]


async def seed_brands(session: AsyncSession) -> None:
    """Seed initial brands only if no brands exist yet (first-ever startup)."""
    from sqlalchemy import func

    count = await session.execute(select(func.count(Brand.id)))
    if (count.scalar() or 0) > 0:
        logger.info("Brands already exist — skipping seed")
        return

    for brand_data in INITIAL_BRANDS:
        session.add(Brand(**brand_data))
        logger.info(f"Seeded brand: {brand_data['name']}")
    await session.commit()


async def seed_retailers(session: AsyncSession) -> None:
    """Seed initial retailers only if no retailers exist yet (first-ever startup)."""
    from sqlalchemy import func

    count = await session.execute(select(func.count(Retailer.id)))
    if (count.scalar() or 0) > 0:
        logger.info("Retailers already exist — skipping seed")
        return

    for retailer_data in INITIAL_RETAILERS:
        session.add(Retailer(**retailer_data))
        logger.info(f"Seeded retailer: {retailer_data['name']}")
    await session.commit()


async def seed_all(session: AsyncSession) -> None:
    await seed_brands(session)
    await seed_retailers(session)


async def get_brand_aliases(brand: Brand) -> list[str]:
    try:
        return json.loads(brand.aliases) if brand.aliases else []
    except json.JSONDecodeError:
        return []


async def get_retailers_for_brand(
    session: AsyncSession, brand_id: int
) -> list[Retailer]:
    result = await session.execute(
        select(Retailer)
        .join(BrandRetailer)
        .where(BrandRetailer.brand_id == brand_id)
    )
    return list(result.scalars().all())
