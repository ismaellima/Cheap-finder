#!/usr/bin/env python
"""Sync all data from production to local dev DB.

Usage:
    python scripts/sync_from_prod.py
    python scripts/sync_from_prod.py --url https://cheap-finder.onrender.com
    python scripts/sync_from_prod.py --password mypass

Fetches /api/export-full from prod and syncs brands, retailers,
brand-retailer mappings, products, and price records into your local
SQLite DB. Existing data is matched by slug/URL — not duplicated.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DEFAULT_PROD_URL = "https://cheap-finder.onrender.com"


async def fetch_export(base_url: str, password: str | None = None) -> dict:
    """Fetch /api/export-full from production."""
    url = f"{base_url.rstrip('/')}/api/export-full"
    logger.info(f"Fetching {url} ...")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        cookies = {}

        # If password protected, authenticate first
        if password:
            login_resp = await client.post(
                f"{base_url.rstrip('/')}/login",
                data={"password": password},
                follow_redirects=False,
            )
            if login_resp.status_code in (302, 303):
                cookies = dict(login_resp.cookies)
            else:
                logger.warning(f"Login returned {login_resp.status_code}, trying without auth")

        resp = await client.get(url, cookies=cookies)

        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"  Got {len(data.get('brands', []))} brands, "
                f"{len(data.get('retailers', []))} retailers, "
                f"{len(data.get('brand_retailers', []))} mappings, "
                f"{len(data.get('products', []))} products, "
                f"{len(data.get('price_records', []))} price records"
            )
            return data
        elif resp.status_code in (401, 403):
            logger.error(
                "Authentication required. Use --password flag."
            )
            sys.exit(1)
        else:
            logger.error(f"Failed to fetch export: HTTP {resp.status_code}")
            logger.error(resp.text[:500])
            sys.exit(1)


async def sync_to_local(data: dict) -> None:
    """Sync all prod data into local DB."""
    from sqlalchemy import select, delete

    from src.db.models import (
        AlertRule,
        Brand,
        BrandRetailer,
        PriceRecord,
        Product,
        Retailer,
    )
    from src.db.session import async_session, init_db

    await init_db()

    async with async_session() as session:
        # ── 1. Sync Brands ──────────────────────────────────────
        brands_data = data.get("brands", [])
        logger.info(f"\n── Syncing {len(brands_data)} brands ──")

        local_brands = await session.execute(select(Brand))
        local_by_slug = {b.slug: b for b in local_brands.scalars().all()}

        b_added, b_updated = 0, 0
        prod_slugs = set()

        for bd in brands_data:
            prod_slugs.add(bd["slug"])
            existing = local_by_slug.get(bd["slug"])

            if existing:
                changed = False
                if existing.name != bd["name"]:
                    existing.name = bd["name"]
                    changed = True
                if existing.category != bd.get("category", ""):
                    existing.category = bd.get("category", "")
                    changed = True
                if existing.alert_threshold_pct != bd.get("alert_threshold_pct", 10.0):
                    existing.alert_threshold_pct = bd.get("alert_threshold_pct", 10.0)
                    changed = True
                new_aliases = json.dumps(bd.get("aliases", []))
                if existing.aliases != new_aliases:
                    existing.aliases = new_aliases
                    changed = True
                if existing.active != bd.get("active", True):
                    existing.active = bd.get("active", True)
                    changed = True
                if changed:
                    b_updated += 1
            else:
                brand = Brand(
                    name=bd["name"],
                    slug=bd["slug"],
                    aliases=json.dumps(bd.get("aliases", [])),
                    category=bd.get("category", ""),
                    alert_threshold_pct=bd.get("alert_threshold_pct", 10.0),
                    active=bd.get("active", True),
                )
                session.add(brand)
                await session.flush()
                # Create default alert rule
                rule = AlertRule(
                    brand_id=brand.id,
                    condition="pct_drop",
                    threshold_pct=bd.get("alert_threshold_pct", 10.0),
                    notify_email=True,
                    notify_dashboard=True,
                )
                session.add(rule)
                b_added += 1

        # Remove brands not in prod
        for slug, brand in local_by_slug.items():
            if slug not in prod_slugs:
                await session.delete(brand)
                b_added -= 1  # offset for logging

        await session.flush()
        logger.info(f"  Brands: {b_added} added, {b_updated} updated")

        # ── 2. Sync Retailers ───────────────────────────────────
        retailers_data = data.get("retailers", [])
        logger.info(f"\n── Syncing {len(retailers_data)} retailers ──")

        local_retailers = await session.execute(select(Retailer))
        local_r_by_slug = {r.slug: r for r in local_retailers.scalars().all()}

        r_added, r_updated = 0, 0
        prod_r_slugs = set()

        for rd in retailers_data:
            prod_r_slugs.add(rd["slug"])
            existing = local_r_by_slug.get(rd["slug"])

            if existing:
                changed = False
                for field, key in [("name", "name"), ("base_url", "base_url"),
                                   ("scraper_type", "scraper_type")]:
                    if getattr(existing, field) != rd.get(key, getattr(existing, field)):
                        setattr(existing, field, rd[key])
                        changed = True
                if existing.active != rd.get("active", True):
                    existing.active = rd.get("active", True)
                    changed = True
                if changed:
                    r_updated += 1
            else:
                retailer = Retailer(
                    name=rd["name"],
                    slug=rd["slug"],
                    base_url=rd["base_url"],
                    scraper_type=rd.get("scraper_type", "generic"),
                    requires_js=rd.get("requires_js", False),
                    active=rd.get("active", True),
                )
                session.add(retailer)
                r_added += 1

        # Remove retailers not in prod
        for slug, retailer in local_r_by_slug.items():
            if slug not in prod_r_slugs:
                await session.delete(retailer)

        await session.flush()
        logger.info(f"  Retailers: {r_added} added, {r_updated} updated")

        # Refresh slug→id mappings after flush
        brands_q = await session.execute(select(Brand))
        brand_by_slug = {b.slug: b for b in brands_q.scalars().all()}

        retailers_q = await session.execute(select(Retailer))
        retailer_by_slug = {r.slug: r for r in retailers_q.scalars().all()}

        # ── 3. Sync BrandRetailer mappings ──────────────────────
        br_data = data.get("brand_retailers", [])
        logger.info(f"\n── Syncing {len(br_data)} brand-retailer mappings ──")

        # Clear existing mappings and re-insert from prod
        await session.execute(delete(BrandRetailer))
        br_added = 0

        for brd in br_data:
            brand = brand_by_slug.get(brd["brand_slug"])
            retailer = retailer_by_slug.get(brd["retailer_slug"])
            if brand and retailer:
                session.add(BrandRetailer(
                    brand_id=brand.id,
                    retailer_id=retailer.id,
                    brand_url=brd.get("brand_url", ""),
                    verified=brd.get("verified", False),
                ))
                br_added += 1

        await session.flush()
        logger.info(f"  Mappings: {br_added} synced")

        # ── 4. Sync Products ───────────────────────────────────
        products_data = data.get("products", [])
        logger.info(f"\n── Syncing {len(products_data)} products ──")

        # Clear price records first (FK dependency), then products
        await session.execute(delete(PriceRecord))
        await session.execute(delete(Product))
        await session.flush()

        p_added = 0
        product_by_url = {}  # for linking price records

        for pd in products_data:
            brand = brand_by_slug.get(pd["brand_slug"])
            retailer = retailer_by_slug.get(pd["retailer_slug"])
            if not brand or not retailer:
                continue

            last_checked = None
            if pd.get("last_checked"):
                try:
                    last_checked = datetime.fromisoformat(pd["last_checked"])
                except (ValueError, TypeError):
                    pass

            created_at = None
            if pd.get("created_at"):
                try:
                    created_at = datetime.fromisoformat(pd["created_at"])
                except (ValueError, TypeError):
                    pass

            product = Product(
                name=pd["name"],
                brand_id=brand.id,
                retailer_id=retailer.id,
                url=pd["url"],
                image_url=pd.get("image_url", ""),
                thumbnail_url=pd.get("thumbnail_url", ""),
                sku=pd.get("sku", ""),
                gender=pd.get("gender", ""),
                sizes=pd.get("sizes", ""),
                current_price=pd.get("current_price"),
                original_price=pd.get("original_price"),
                on_sale=pd.get("on_sale", False),
                tracked=pd.get("tracked", True),
                last_checked=last_checked,
            )
            if created_at:
                product.created_at = created_at

            session.add(product)
            await session.flush()
            product_by_url[pd["url"]] = product
            p_added += 1

        logger.info(f"  Products: {p_added} synced")

        # ── 5. Sync Price Records ──────────────────────────────
        prices_data = data.get("price_records", [])
        logger.info(f"\n── Syncing {len(prices_data)} price records ──")

        pr_added = 0
        for prd in prices_data:
            product = product_by_url.get(prd["product_url"])
            if not product:
                continue

            recorded_at = None
            if prd.get("recorded_at"):
                try:
                    recorded_at = datetime.fromisoformat(prd["recorded_at"])
                except (ValueError, TypeError):
                    pass

            record = PriceRecord(
                product_id=product.id,
                price=prd["price"],
                original_price=prd.get("original_price"),
                on_sale=prd.get("on_sale", False),
                currency=prd.get("currency", "CAD"),
            )
            if recorded_at:
                record.recorded_at = recorded_at

            session.add(record)
            pr_added += 1

        await session.commit()
        logger.info(f"  Price records: {pr_added} synced")

        logger.info(f"\n✓ Sync complete!")
        logger.info(f"  {b_added + b_updated} brands, {r_added + r_updated} retailers")
        logger.info(f"  {br_added} mappings, {p_added} products, {pr_added} price records")


def main():
    parser = argparse.ArgumentParser(description="Sync all prod data to local dev DB")
    parser.add_argument(
        "--url",
        default=DEFAULT_PROD_URL,
        help=f"Production URL (default: {DEFAULT_PROD_URL})",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Dashboard password if auth is enabled",
    )
    args = parser.parse_args()

    async def run():
        data = await fetch_export(args.url, args.password)
        await sync_to_local(data)

    asyncio.run(run())


if __name__ == "__main__":
    main()
