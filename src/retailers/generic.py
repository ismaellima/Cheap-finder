from __future__ import annotations

import logging
import re

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct
from src.retailers.shopify_base import ShopifyBase

logger = logging.getLogger(__name__)


class GenericScraper(ShopifyBase):
    """Generic scraper that tries Shopify endpoints first, then falls back
    to meta tags / JSON-LD for price extraction.

    Most new retailers added via the UI are Shopify stores, so the Shopify
    search_brand() and collection endpoints work out of the box. For
    non-Shopify sites, the Shopify endpoints will return errors and the
    scraper gracefully returns empty results.
    """

    name = "Generic"
    slug = "generic"
    base_url = ""
    requires_js = False

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        """Try Shopify JSON endpoint first, then fall back to meta/JSON-LD."""
        # Try Shopify .json endpoint
        result = await super().get_price(product_url)
        if result is not None:
            return result

        # Fallback: parse HTML for meta tags and JSON-LD
        try:
            soup = await self._fetch_soup(product_url)
        except Exception:
            logger.exception(f"Failed to fetch {product_url}")
            return None

        price = self._extract_price_from_meta(soup)
        if price is None:
            price = self._extract_price_from_json_ld(soup)

        if price is None:
            return None

        return ScrapedPrice(price=price)

    def _extract_price_from_meta(self, soup) -> int | None:
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            return self.parse_price(meta["content"])

        meta = soup.find("meta", {"property": "og:price:amount"})
        if meta and meta.get("content"):
            return self.parse_price(meta["content"])

        return None

    def _extract_price_from_json_ld(self, soup) -> int | None:
        import json

        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0]

                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}

                price_str = str(offers.get("price", ""))
                if price_str:
                    return self.parse_price(price_str)
            except (json.JSONDecodeError, AttributeError, IndexError):
                continue

        return None
