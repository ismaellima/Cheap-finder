"""Simons (simons.ca) scraper.

Platform: Custom. Uses HTML parsing with BeautifulSoup.
Brand search via: /en/search?query={brand}
Product pages have meta tags and JSON-LD for price extraction.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class SimonsScraper(RetailerBase):
    name = "Simons"
    slug = "simons"
    base_url = "https://www.simons.ca"
    requires_js = False

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        encoded = urllib.parse.quote(brand_name)
        url = f"{self.base_url}/en/search?query={encoded}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.exception(f"{self.name}: Failed to search for '{brand_name}'")
            return []

        products: list[ScrapedProduct] = []

        # Look for product cards in search results
        cards = soup.select("[data-product-tile], .product-tile, .product-card, article[data-product]")
        if not cards:
            # Try broader selectors
            cards = soup.select("a[href*='/product/'], a[href*='/products/']")

        for card in cards[:50]:  # Limit to 50 products
            scraped = self._parse_product_card(card)
            if scraped:
                products.append(scraped)

        # If no products found via cards, try JSON-LD
        if not products:
            products = self._extract_from_json_ld(soup)

        logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}'")
        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        try:
            soup = await self._fetch_soup(product_url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch {product_url}")
            return None

        # Try JSON-LD first
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
                currency = "CAD"
                cur_meta = soup.find("meta", {"property": "product:price:currency"})
                if cur_meta and cur_meta.get("content"):
                    currency = cur_meta["content"]
                return ScrapedPrice(price=price, currency=currency)

        # Try price elements in HTML
        for selector in [".product-price", ".price", "[data-price]", ".current-price"]:
            el = soup.select_one(selector)
            if el:
                price = self.parse_price(el.get_text())
                if price:
                    return ScrapedPrice(price=price)

        return None

    def _parse_product_card(self, card) -> ScrapedProduct | None:
        # Get product URL
        link = card if card.name == "a" else card.find("a")
        if not link or not link.get("href"):
            return None

        href = link["href"]
        if not href.startswith("http"):
            href = f"{self.base_url}{href}"

        # Get product name
        name_el = card.select_one(".product-name, .product-title, h3, h4, [data-product-name]")
        name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)
        if not name or len(name) < 3:
            return None

        # Get price
        price_el = card.select_one(".product-price, .price, [data-price], .current-price")
        price = None
        if price_el:
            price = self.parse_price(price_el.get_text())

        if price is None:
            return None

        # Get image
        img = card.find("img")
        image_url = ""
        if img:
            image_url = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
            if image_url.startswith("//"):
                image_url = "https:" + image_url

        return ScrapedProduct(
            name=name,
            url=href,
            price=price,
            image_url=image_url,
            thumbnail_url=image_url,
        )

    def _extract_from_json_ld(self, soup) -> list[ScrapedProduct]:
        products: list[ScrapedProduct] = []
        scripts = soup.find_all("script", {"type": "application/ld+json"})

        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    for item in data:
                        p = self._json_ld_to_product(item)
                        if p:
                            products.append(p)
                elif isinstance(data, dict):
                    if data.get("@type") == "ItemList":
                        for entry in data.get("itemListElement", []):
                            item = entry.get("item", entry)
                            p = self._json_ld_to_product(item)
                            if p:
                                products.append(p)
                    else:
                        p = self._json_ld_to_product(data)
                        if p:
                            products.append(p)
            except (json.JSONDecodeError, AttributeError):
                continue

        return products

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
            name=name,
            url=url,
            price=price,
            image_url=image,
            thumbnail_url=image,
        )
