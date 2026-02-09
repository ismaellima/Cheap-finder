#!/usr/bin/env python
"""Sync brands and retailers from production to local dev DB.

Usage:
    python scripts/sync_from_prod.py
    python scripts/sync_from_prod.py --url https://cheap-finder.onrender.com

This fetches /api/export from prod and upserts brands + retailers
into your local SQLite DB. Existing data is updated, not duplicated.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DEFAULT_PROD_URL = "https://cheap-finder.onrender.com"


async def fetch_export(base_url: str, password: str | None = None) -> dict:
    """Fetch /api/export from production."""
    url = f"{base_url.rstrip('/')}/api/export"
    logger.info(f"Fetching {url} ...")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        headers = {}
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

        resp = await client.get(url, headers=headers, cookies=cookies)

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401 or resp.status_code == 403:
            logger.error(
                "Authentication required. Set DASHBOARD_PASSWORD env var or use --password flag."
            )
            sys.exit(1)
        else:
            logger.error(f"Failed to fetch export: HTTP {resp.status_code}")
            logger.error(resp.text[:500])
            sys.exit(1)


async def sync_to_local(data: dict) -> None:
    """Upsert brands and retailers into local DB."""
    from src.db.models import AlertRule, Brand, Retailer
    from src.db.session import async_session, init_db
    from sqlalchemy import select

    await init_db()

    async with async_session() as session:
        # --- Sync Brands ---
        brands = data.get("brands", [])
        logger.info(f"\nSyncing {len(brands)} brands...")

        local_brands = await session.execute(select(Brand))
        local_by_slug = {b.slug: b for b in local_brands.scalars().all()}

        added, updated, removed = 0, 0, 0
        prod_slugs = set()

        for bd in brands:
            prod_slugs.add(bd["slug"])
            existing = local_by_slug.get(bd["slug"])

            if existing:
                # Update existing brand
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
                    updated += 1
                    logger.info(f"  Updated: {bd['name']}")
            else:
                # Create new brand
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
                added += 1
                logger.info(f"  Added: {bd['name']}")

        # Remove brands that are no longer in prod
        for slug, brand in local_by_slug.items():
            if slug not in prod_slugs:
                await session.delete(brand)
                removed += 1
                logger.info(f"  Removed: {brand.name}")

        logger.info(f"  Brands: {added} added, {updated} updated, {removed} removed")

        # --- Sync Retailers ---
        retailers = data.get("retailers", [])
        logger.info(f"\nSyncing {len(retailers)} retailers...")

        local_retailers = await session.execute(select(Retailer))
        local_r_by_slug = {r.slug: r for r in local_retailers.scalars().all()}

        r_added, r_updated, r_removed = 0, 0, 0
        prod_r_slugs = set()

        for rd in retailers:
            prod_r_slugs.add(rd["slug"])
            existing = local_r_by_slug.get(rd["slug"])

            if existing:
                changed = False
                if existing.name != rd["name"]:
                    existing.name = rd["name"]
                    changed = True
                if existing.base_url != rd["base_url"]:
                    existing.base_url = rd["base_url"]
                    changed = True
                if existing.scraper_type != rd.get("scraper_type", "generic"):
                    existing.scraper_type = rd.get("scraper_type", "generic")
                    changed = True
                if existing.active != rd.get("active", True):
                    existing.active = rd.get("active", True)
                    changed = True
                if changed:
                    r_updated += 1
                    logger.info(f"  Updated: {rd['name']}")
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
                logger.info(f"  Added: {rd['name']}")

        # Remove retailers that are no longer in prod
        for slug, retailer in local_r_by_slug.items():
            if slug not in prod_r_slugs:
                await session.delete(retailer)
                r_removed += 1
                logger.info(f"  Removed: {retailer.name}")

        logger.info(f"  Retailers: {r_added} added, {r_updated} updated, {r_removed} removed")

        await session.commit()
        logger.info("\nSync complete!")


def main():
    parser = argparse.ArgumentParser(description="Sync prod data to local dev DB")
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
