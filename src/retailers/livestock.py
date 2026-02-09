"""Livestock / Deadstock (deadstock.ca) scraper.

Platform: Shopify Classic with Algolia search.
The /products.json endpoint works for all products.
Collection-scoped JSON may be empty due to Algolia, so we filter by vendor.
"""
from __future__ import annotations

import logging

from src.retailers.shopify_base import ShopifyBase
from src.retailers.base import ScrapedProduct

logger = logging.getLogger(__name__)


class LivestockScraper(ShopifyBase):
    name = "Livestock"
    slug = "livestock"
    base_url = "https://www.deadstock.ca"
    requires_js = False

    brand_slug_map = {
        "arc'teryx": "arcteryx",
        "arcteryx": "arcteryx",
        "new balance": "new-balance",
        "on cloud": "on",
        "on running": "on",
    }

    # Map brand search terms to Shopify vendor names (case-insensitive match)
    vendor_map = {
        "arc'teryx": ["arcteryx", "arc'teryx"],
        "arcteryx": ["arcteryx", "arc'teryx"],
        "new balance": ["new balance"],
        "on cloud": ["on", "on running"],
        "on running": ["on", "on running"],
    }

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        """Search by collection first, then fall back to filtering /products.json by vendor."""
        # Try collection first (via parent class)
        products = await super().search_brand(brand_name)
        if products:
            return products

        # Fallback: fetch all products and filter by vendor
        logger.info(f"{self.name}: Collection empty, trying /products.json vendor filter")
        vendor_names = self.vendor_map.get(brand_name.lower(), [brand_name.lower()])

        all_products: list[ScrapedProduct] = []
        page = 1
        while page <= 5:  # Cap at 5 pages to avoid hammering
            url = f"{self.base_url}/products.json?limit=250&page={page}"
            data = await self._fetch_json(url)
            if not data or not isinstance(data, dict):
                break

            page_products = data.get("products", [])
            if not page_products:
                break

            for p in page_products:
                vendor = (p.get("vendor") or "").lower()
                if any(v in vendor for v in vendor_names):
                    scraped = self._parse_shopify_product(p)
                    if scraped:
                        all_products.append(scraped)

            page += 1

        logger.info(f"{self.name}: Found {len(all_products)} products for '{brand_name}' via vendor filter")
        return all_products
