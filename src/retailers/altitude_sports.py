"""Altitude Sports (altitude-sports.com) scraper.

Platform: Custom React SPA. Product data is loaded via JavaScript.
Brand pages: /c/{brand-slug}
Since this is a SPA, we attempt to extract any server-side rendered data
from the initial HTML, and fall back to Playwright if needed.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class AltitudeSportsScraper(RetailerBase):
    name = "Altitude Sports"
    slug = "altitude_sports"
    base_url = "https://www.altitude-sports.com"
    requires_js = True  # SPA — may need Playwright for full scraping

    brand_slug_map = {
        "on cloud": "on",
        "on running": "on",
        "new balance": "new-balance",
        "arc'teryx": "arcteryx",
        "arcteryx": "arcteryx",
        "satisfy": "satisfy",
        "satisfy running": "satisfy",
        "a.p.c.": "apc",
        "apc": "apc",
        "sabre": "sabre",
    }

    def _brand_to_slug(self, brand_name: str) -> str:
        lower = brand_name.lower()
        for key, slug in self.brand_slug_map.items():
            if key in lower or lower in key:
                return slug
        return re.sub(r"[^a-z0-9]+", "-", lower).strip("-")

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        slug = self._brand_to_slug(brand_name)
        url = f"{self.base_url}/c/{slug}"

        try:
            html = await self._fetch(url)
        except Exception:
            logger.warning(f"{self.name}: Failed to fetch brand page for '{brand_name}'")
            return []

        products = self._extract_from_html(html)

        if not products:
            # Try search endpoint
            search_url = f"{self.base_url}/search?query={urllib.parse.quote(brand_name)}"
            try:
                html = await self._fetch(search_url)
                products = self._extract_from_html(html)
            except Exception:
                logger.warning(f"{self.name}: Search also failed for '{brand_name}'")

        logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}'")
        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        try:
            soup = await self._fetch_soup(product_url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch {product_url}")
            return None

        # Try JSON-LD
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") == "Product":
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = self.parse_price(str(offers.get("price", "")))
                    if price:
                        return ScrapedPrice(
                            price=price,
                            currency=offers.get("priceCurrency", "CAD"),
                            available="InStock" in str(offers.get("availability", "")),
                        )
            except (json.JSONDecodeError, AttributeError):
                continue

        # Try meta tags
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            price = self.parse_price(meta["content"])
            if price:
                return ScrapedPrice(price=price)

        return None

    def _extract_from_html(self, html: str) -> list[ScrapedProduct]:
        """Try to extract product data from server-side rendered HTML or embedded JSON."""
        products: list[ScrapedProduct] = []

        # Look for embedded JSON data (React apps often embed initial state)
        patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>',
            r'window\.__NEXT_DATA__\s*=\s*(\{.+?\})\s*;?\s*</script>',
            r'"products"\s*:\s*(\[.+?\])',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    if isinstance(data, list):
                        for item in data:
                            p = self._parse_product_data(item)
                            if p:
                                products.append(p)
                    elif isinstance(data, dict):
                        # Navigate through nested structures
                        product_list = self._find_products_in_dict(data)
                        for item in product_list:
                            p = self._parse_product_data(item)
                            if p:
                                products.append(p)
                except (json.JSONDecodeError, KeyError):
                    continue

        # Fallback: parse JSON-LD
        if not products:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            scripts = soup.find_all("script", {"type": "application/ld+json"})
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get("@type") == "ItemList":
                        for entry in data.get("itemListElement", []):
                            item = entry.get("item", entry)
                            p = self._parse_product_data(item)
                            if p:
                                products.append(p)
                except (json.JSONDecodeError, AttributeError):
                    continue

        return products

    def _find_products_in_dict(self, data: dict, depth: int = 0) -> list[dict]:
        """Recursively find product arrays in nested dict."""
        if depth > 5:
            return []

        products = []
        for key, value in data.items():
            if key in ("products", "items", "results") and isinstance(value, list):
                return value
            elif isinstance(value, dict):
                found = self._find_products_in_dict(value, depth + 1)
                if found:
                    return found

        return products

    def _parse_product_data(self, data: dict) -> ScrapedProduct | None:
        name = data.get("name") or data.get("title", "")
        if not name:
            return None

        url = data.get("url") or data.get("href", "")
        slug = data.get("slug") or data.get("handle", "")
        if slug and not url:
            url = f"{self.base_url}/products/{slug}"
        if url and not url.startswith("http"):
            url = f"{self.base_url}{url}"

        # Extract price
        offers = data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        price = (
            self.parse_price(str(offers.get("price", "")))
            or self.parse_price(str(data.get("price", "")))
            or self.parse_price(str(data.get("salePrice", "")))
        )

        if price is None:
            return None

        image = data.get("image", "") or data.get("imageUrl", "")
        if isinstance(image, list):
            image = image[0] if image else ""

        return ScrapedProduct(
            name=name,
            url=url,
            price=price,
            image_url=image,
            thumbnail_url=image,
        )
