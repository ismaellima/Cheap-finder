from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Brand, BrandRetailer, Retailer

logger = logging.getLogger(__name__)


async def _fetch_prod_data(prod_url: str) -> dict:
    """Fetch brands and retailers from the live prod instance via /api/export.

    This 'rescues' any brands/retailers added via the UI before the DB is wiped
    on Render free tier deploys. Returns empty dict on failure (non-blocking).
    """
    import httpx

    export_url = f"{prod_url.rstrip('/')}/api/export"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(export_url)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    f"Auto-rescue: fetched {len(data.get('brands', []))} brands, "
                    f"{len(data.get('retailers', []))} retailers from prod"
                )
                return data
            else:
                logger.warning(f"Auto-rescue: /api/export returned {resp.status_code}")
    except Exception:
        logger.warning("Auto-rescue: could not reach prod instance (may be first deploy)")
    return {}

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
        "name": "Beams",
        "slug": "beams",
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
        "name": "Ciele",
        "slug": "ciele",
        "aliases": json.dumps([]),
        "category": "outdoor",
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
        "name": "Keen",
        "slug": "keen",
        "aliases": json.dumps([]),
        "category": "footwear",
    },
    {
        "name": "Kitowa",
        "slug": "kitowa",
        "aliases": json.dumps([]),
        "category": "Perfume",
    },
    {
        "name": "Koumori",
        "slug": "koumori",
        "aliases": json.dumps([]),
        "category": "running",
    },
    {
        "name": "Nanga",
        "slug": "nanga",
        "aliases": json.dumps([]),
        "category": "fashion",
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
    {
        "name": "Tekla",
        "slug": "tekla",
        "aliases": json.dumps([]),
        "category": "home",
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
        "name": "Annms",
        "slug": "annms",
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
        "name": "Boutique archive",
        "slug": "boutique-archive",
        "base_url": "https://www.boutiquearchive.com",
        "scraper_type": "generic",
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
        "name": "Culture athletics",
        "slug": "culture-athletics",
        "base_url": "https://cultureathletics.com",
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
        "name": "Gravity pope",
        "slug": "gravity-pope",
        "base_url": "https://www.gravitypope.com",
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
        "name": "Lost and found",
        "slug": "lost-and-found",
        "base_url": "https://shoplostfound.com",
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
        "name": "Nomad",
        "slug": "nomad",
        "base_url": "https://nomadshop.net",
        "scraper_type": "generic",
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
        "name": "O lodge",
        "slug": "o-lodge",
        "base_url": "https://olodge.ca",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "Out and about",
        "slug": "out-and-about",
        "base_url": "https://outnaboutboutique.com",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "Run parlour",
        "slug": "run-parlour",
        "base_url": "https://runparlour.com",
        "scraper_type": "generic",
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
        "name": "Stomping ground",
        "slug": "stomping-ground",
        "base_url": "https://stompingground.ca",
        "scraper_type": "generic",
        "requires_js": False,
    },
    {
        "name": "The last hunt",
        "slug": "the-last-hunt",
        "base_url": "https://www.thelasthunt.com",
        "scraper_type": "the_last_hunt",
        "requires_js": False,
    },
    {
        "name": "TNT",
        "slug": "tnt",
        "base_url": "https://tntfashion.ca",
        "scraper_type": "generic",
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


async def seed_brands(
    session: AsyncSession, extra_brands: list[dict] | None = None
) -> None:
    """Upsert seed brands — add missing ones by slug, never delete existing.

    Merges hardcoded INITIAL_BRANDS with any extra brands (e.g. from prod rescue).
    This ensures all expected brands exist after a DB wipe (Render free tier)
    while preserving any brands added via the UI.
    """
    # Merge: hardcoded list first, then extras (dedup by slug)
    all_brands = list(INITIAL_BRANDS)
    seen_slugs = {b["slug"] for b in all_brands}
    for eb in extra_brands or []:
        slug = eb.get("slug") or re.sub(r"[^a-z0-9]+", "-", eb["name"].lower()).strip("-")
        if slug not in seen_slugs:
            # Convert prod export format to seed format
            all_brands.append({
                "name": eb["name"],
                "slug": slug,
                "aliases": json.dumps(eb.get("aliases", [])) if isinstance(eb.get("aliases"), list) else eb.get("aliases", ""),
                "category": eb.get("category", ""),
                "alert_threshold_pct": eb.get("alert_threshold_pct", 10.0),
            })
            seen_slugs.add(slug)

    added = 0
    for brand_data in all_brands:
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


async def seed_retailers(
    session: AsyncSession, extra_retailers: list[dict] | None = None
) -> None:
    """Upsert seed retailers — add missing ones by slug, never delete existing.

    Merges hardcoded INITIAL_RETAILERS with any extra retailers (e.g. from prod rescue).
    This ensures all expected retailers exist after a DB wipe (Render free tier)
    while preserving any retailers added via the UI.
    """
    # Merge: hardcoded list first, then extras (dedup by slug)
    all_retailers = list(INITIAL_RETAILERS)
    seen_slugs = {r["slug"] for r in all_retailers}
    for er in extra_retailers or []:
        slug = er.get("slug") or re.sub(r"[^a-z0-9]+", "-", er["name"].lower()).strip("-")
        if slug not in seen_slugs:
            # Convert prod export format to seed format
            all_retailers.append({
                "name": er["name"],
                "slug": slug,
                "base_url": er.get("base_url", ""),
                "scraper_type": er.get("scraper_type", "generic"),
                "requires_js": er.get("requires_js", False),
            })
            seen_slugs.add(slug)

    added = 0
    updated = 0
    for retailer_data in all_retailers:
        existing = await session.execute(
            select(Retailer).where(Retailer.slug == retailer_data["slug"])
        )
        retailer = existing.scalar_one_or_none()
        if retailer is None:
            session.add(Retailer(**retailer_data))
            logger.info(f"Seeded retailer: {retailer_data['name']}")
            added += 1
        else:
            # Update scraper_type if it changed (e.g. generic → dedicated scraper)
            new_type = retailer_data.get("scraper_type", "generic")
            if retailer.scraper_type != new_type and new_type != "generic":
                logger.info(
                    f"Updated {retailer.name} scraper_type: "
                    f"{retailer.scraper_type} → {new_type}"
                )
                retailer.scraper_type = new_type
                updated += 1
    if added or updated:
        await session.commit()
        if added:
            logger.info(f"Seeded {added} new retailers")
        if updated:
            logger.info(f"Updated {updated} retailer scraper types")
    else:
        logger.info("All seed retailers already exist — nothing to add")


async def seed_all(session: AsyncSession) -> None:
    """Seed brands and retailers, rescuing any UI-added data from prod first."""
    from src.config import settings

    # If running on Render, fetch current prod data before seeding
    # This preserves brands/retailers added via the UI that aren't in the hardcoded lists
    prod_brands: list[dict] = []
    prod_retailers: list[dict] = []
    if settings.RENDER_EXTERNAL_URL:
        prod_data = await _fetch_prod_data(settings.RENDER_EXTERNAL_URL)
        prod_brands = prod_data.get("brands", [])
        prod_retailers = prod_data.get("retailers", [])

    await seed_brands(session, extra_brands=prod_brands)
    await seed_retailers(session, extra_retailers=prod_retailers)


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
