from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, Retailer

logger = logging.getLogger(__name__)

INITIAL_BRANDS = [
    {
        "name": "APFR",
        "slug": "apfr",
        "aliases": json.dumps(["A.P.C.", "A.P.C", "APC"]),
        "category": "fashion",
    },
    {
        "name": "Arc'teryx",
        "slug": "arcteryx",
        "aliases": json.dumps(["Arcteryx", "Arc'teryx"]),
        "category": "outdoor",
    },
    {
        "name": "Balmoral",
        "slug": "balmoral",
        "aliases": json.dumps([]),
        "category": "fashion",
    },
    {
        "name": "Beams Plus",
        "slug": "beams-plus",
        "aliases": json.dumps([]),
        "category": "fashion",
    },
    {
        "name": "District Vision",
        "slug": "district-vision",
        "aliases": json.dumps([]),
        "category": "outdoor",
    },
    {
        "name": "Goldwin",
        "slug": "goldwin",
        "aliases": json.dumps([]),
        "category": "fashion",
    },
    {
        "name": "Koumori",
        "slug": "koumori",
        "aliases": json.dumps([]),
        "category": "running",
    },
    {
        "name": "New Balance",
        "slug": "new-balance",
        "aliases": json.dumps(["NB", "New Balance Made in USA", "New Balance Made in UK"]),
        "category": "sneakers",
    },
    {
        "name": "On Cloud",
        "slug": "on-cloud",
        "aliases": json.dumps(["On Running", "On"]),
        "category": "running",
    },
    {
        "name": "Patagonia",
        "slug": "patagonia",
        "aliases": json.dumps([]),
        "category": "outdoor",
    },
    {
        "name": "Satisfy Running",
        "slug": "satisfy-running",
        "aliases": json.dumps(["Satisfy"]),
        "category": "running",
    },
]

INITIAL_RETAILERS = [
    {
        "name": "Altitude Sports",
        "slug": "altitude-sports",
        "base_url": "https://www.altitude-sports.com",
        "scraper_type": "altitude_sports",
        "requires_js": False,
    },
    {
        "name": "Annmz",
        "slug": "annmz",
        "base_url": "https://www.annmsshop.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "Blue Button Shop",
        "slug": "bluebuttonshop",
        "base_url": "https://www.bluebuttonshop.com",
        "scraper_type": "bluebuttonshop",
        "requires_js": False,
    },
    {
        "name": "Capsule Toronto",
        "slug": "capsule-toronto",
        "base_url": "https://www.capsuletoronto.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "Empire",
        "slug": "empire",
        "base_url": "https://www.thinkempire.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "En route run",
        "slug": "en-route-run",
        "base_url": "https://www.enroute.run",
        "scraper_type": "generic",
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
        "name": "Le club",
        "slug": "le-club",
        "base_url": "https://leclub.cc",
        "scraper_type": "generic",
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
        "name": "Lopez",
        "slug": "lopez",
        "base_url": "https://www.lopezmtl.ca",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "Muddy George",
        "slug": "muddy-george",
        "base_url": "https://muddygeorge.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "NRML",
        "slug": "nrml",
        "base_url": "https://nrml.ca",
        "scraper_type": "nrml",
        "requires_js": False,
    },
    {
        "name": "Nordstrom",
        "slug": "nordstrom",
        "base_url": "https://www.nordstrom.ca",
        "scraper_type": "nordstrom",
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
        "name": "Simons",
        "slug": "simons",
        "base_url": "https://www.simons.ca",
        "scraper_type": "simons",
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
        "name": "Wallace",
        "slug": "wallace",
        "base_url": "https://wallacemercantileshop.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
]


async def seed_brands(session: AsyncSession) -> None:
    """Upsert seed brands — add missing ones by slug, never delete existing.

    This ensures all expected brands exist after a DB wipe (Render free tier)
    while preserving any brands added via the UI that aren't in this list.
    """
    added = 0
    for brand_data in INITIAL_BRANDS:
        existing = await session.execute(
            select(Brand).where(Brand.slug == brand_data["slug"])
        )
        if existing.scalar_one_or_none() is None:
            session.add(Brand(**brand_data))
            logger.info(f"Seeded brand: {brand_data['name']}")
            added += 1
    if added:
        await session.commit()
        logger.info(f"Seeded {added} new brands")
    else:
        logger.info("All seed brands already exist — nothing to add")


async def seed_retailers(session: AsyncSession) -> None:
    """Upsert seed retailers — add missing ones by slug, never delete existing.

    This ensures all expected retailers exist after a DB wipe (Render free tier)
    while preserving any retailers added via the UI that aren't in this list.
    """
    added = 0
    for retailer_data in INITIAL_RETAILERS:
        existing = await session.execute(
            select(Retailer).where(Retailer.slug == retailer_data["slug"])
        )
        if existing.scalar_one_or_none() is None:
            session.add(Retailer(**retailer_data))
            logger.info(f"Seeded retailer: {retailer_data['name']}")
            added += 1
    if added:
        await session.commit()
        logger.info(f"Seeded {added} new retailers")
    else:
        logger.info("All seed retailers already exist — nothing to add")


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
