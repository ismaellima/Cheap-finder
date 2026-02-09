from __future__ import annotations

import logging
import re

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class GenericScraper(RetailerBase):
    name = "Generic"
    slug = "generic"
    base_url = ""
    requires_js = False

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        logger.warning(f"Generic scraper cannot search brands — override for specific retailers")
        return []

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
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
