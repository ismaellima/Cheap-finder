"""Altitude Sports (altitude-sports.com) scraper.

Platform: Next.js SPA with Algolia search. Product data is embedded in
__NEXT_DATA__ within serverState.initialResults (Algolia InstantSearch).
Search endpoint: /search?query={brand}
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
    requires_js = False  # __NEXT_DATA__ is in initial HTML

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        search_url = (
            f"{self.base_url}/search?query={urllib.parse.quote(brand_name)}"
        )

        try:
            html = await self._fetch(search_url)
        except Exception:
            logger.warning(
                f"{self.name}: Failed to fetch search page for '{brand_name}'"
            )
            return []

        products = self._extract_from_next_data(html)
        logger.info(
            f"{self.name}: Found {len(products)} products for '{brand_name}'"
        )
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
                            available="InStock"
                            in str(offers.get("availability", "")),
                        )
            except (json.JSONDecodeError, AttributeError):
                continue

        # Try __NEXT_DATA__ for product page
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', soup.text, re.DOTALL
        )
        if match:
            try:
                nd = json.loads(match.group(1))
                pp = nd.get("props", {}).get("pageProps", {})
                product_data = pp.get("product", {})
                if product_data:
                    price_obj = product_data.get("price", {}).get("CAD", {})
                    cents_list = price_obj.get("centAmount", [])
                    if cents_list:
                        price = cents_list[0] if isinstance(cents_list, list) else cents_list
                        orig_obj = product_data.get("original_price", {}).get("CAD", {})
                        orig_cents = orig_obj.get("centAmount", [])
                        orig = orig_cents[0] if orig_cents and isinstance(orig_cents, list) else None
                        on_sale = orig is not None and orig > price
                        return ScrapedPrice(
                            price=price,
                            original_price=orig if on_sale else None,
                            on_sale=on_sale,
                            currency="CAD",
                            available=True,
                        )
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        # Try meta tags
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            price = self.parse_price(meta["content"])
            if price:
                return ScrapedPrice(price=price)

        return None

    def _extract_from_next_data(self, html: str) -> list[ScrapedProduct]:
        """Extract products from __NEXT_DATA__ Algolia search results."""
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if not match:
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        page_props = data.get("props", {}).get("pageProps", {})
        server_state = page_props.get("serverState", {})
        initial_results = server_state.get("initialResults", {})

        products: list[ScrapedProduct] = []

        for key, value in initial_results.items():
            if not isinstance(value, dict):
                continue
            results = value.get("results", [])
            for result_group in results:
                if not isinstance(result_group, dict):
                    continue
                for hit in result_group.get("hits", []):
                    p = self._parse_algolia_hit(hit)
                    if p:
                        products.append(p)

        return products

    @staticmethod
    def _extract_cents(price_obj) -> int | None:
        """Extract price in cents from Altitude Sports price structure."""
        if not isinstance(price_obj, dict):
            return None
        cad = price_obj.get("CAD", {})
        if not isinstance(cad, dict):
            return None
        cents = cad.get("centAmount", [])
        if isinstance(cents, list):
            return cents[0] if cents else None
        if isinstance(cents, (int, float)):
            return int(cents)
        return None

    def _parse_algolia_hit(self, hit: dict) -> ScrapedProduct | None:
        name = hit.get("name", "")
        slug = hit.get("slug", "")
        if not name or not slug:
            return None

        url = f"{self.base_url}/products/{slug}"

        # Price is stored as {"CAD": {"centAmount": [12999]}}
        price = self._extract_cents(hit.get("price", {}))
        if price is None:
            return None

        # Original price
        original_price = self._extract_cents(hit.get("original_price", {}))

        on_sale = original_price is not None and original_price > price
        discount = hit.get("discounted_percent", 0)
        if isinstance(discount, list):
            discount = discount[0] if discount else 0
        if discount and isinstance(discount, (int, float)) and discount > 0:
            on_sale = True

        image_url = hit.get("image_url", "")
        thumbnails = hit.get("thumbnails", {})
        if isinstance(thumbnails, dict):
            thumbnail_url = thumbnails.get("small", image_url)
        elif isinstance(thumbnails, list) and thumbnails:
            thumbnail_url = thumbnails[0] if isinstance(thumbnails[0], str) else image_url
        else:
            thumbnail_url = image_url

        return ScrapedProduct(
            name=name,
            url=url,
            price=price,
            original_price=original_price if on_sale else None,
            on_sale=on_sale,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
        )
