"""Nordstrom (nordstrom.ca) scraper.

Uses search and product pages with JSON-LD and meta tag extraction.
"""
from __future__ import annotations

import json
import logging
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class NordstromScraper(RetailerBase):
    name = "Nordstrom"
    slug = "nordstrom"
    base_url = "https://www.nordstrom.ca"
    requires_js = False

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        encoded = urllib.parse.quote(brand_name)
        url = f"{self.base_url}/sr?keyword={encoded}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.warning(f"{self.name}: Failed to search for '{brand_name}'")
            return []

        products: list[ScrapedProduct] = []

        # Try JSON-LD ItemList
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for entry in data.get("itemListElement", []):
                        item = entry.get("item", entry)
                        p = self._json_ld_to_product(item)
                        if p:
                            products.append(p)
            except (json.JSONDecodeError, AttributeError):
                continue

        # Try product cards
        if not products:
            cards = soup.select("article[data-product], .product-card, [data-product-card]")
            for card in cards[:50]:
                p = self._parse_card(card)
                if p:
                    products.append(p)

        logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}'")
        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        try:
            soup = await self._fetch_soup(product_url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch {product_url}")
            return None

        # JSON-LD
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

        # Meta tags
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            price = self.parse_price(meta["content"])
            if price:
                return ScrapedPrice(price=price)

        return None

    def _json_ld_to_product(self, data: dict) -> ScrapedProduct | None:
        if data.get("@type") != "Product":
            return None

        name = data.get("name", "")
        url = data.get("url", "")
        if not name:
            return None

        offers = data.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        price = self.parse_price(str(offers.get("price", "")))
        if price is None:
            return None

        image = data.get("image", "")
        if isinstance(image, list):
            image = image[0] if image else ""

        if url and not url.startswith("http"):
            url = f"{self.base_url}{url}"

        return ScrapedProduct(
            name=name, url=url, price=price,
            image_url=image, thumbnail_url=image,
        )

    def _parse_card(self, card) -> ScrapedProduct | None:
        link = card.find("a")
        if not link or not link.get("href"):
            return None

        href = link["href"]
        if not href.startswith("http"):
            href = f"{self.base_url}{href}"

        name_el = card.select_one("h3, h4, .product-name, [data-product-name]")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            return None

        price = None
        for sel in [".price", "[data-price]", ".current-price"]:
            el = card.select_one(sel)
            if el:
                price = self.parse_price(el.get_text())
                if price:
                    break

        if price is None:
            return None

        img = card.find("img")
        image_url = ""
        if img:
            image_url = img.get("src") or img.get("data-src") or ""

        return ScrapedProduct(
            name=name, url=href, price=price,
            image_url=image_url, thumbnail_url=image_url,
        )
