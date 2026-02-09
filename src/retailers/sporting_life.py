"""Sporting Life (sportinglife.ca) scraper.

Platform: Salesforce Commerce Cloud (Demandware).
Search: /en-CA/search?q={query}&prefn1=brand&prefv1={brand}
Product pages have JSON-LD structured data.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class SportingLifeScraper(RetailerBase):
    name = "Sporting Life"
    slug = "sporting_life"
    base_url = "https://www.sportinglife.ca"
    requires_js = False  # Try static first, fall back if needed

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        encoded = urllib.parse.quote(brand_name)
        url = f"{self.base_url}/en-CA/search?q={encoded}&prefn1=brand&prefv1={encoded}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.warning(f"{self.name}: Failed to search for '{brand_name}'")
            return []

        products: list[ScrapedProduct] = []

        # SFCC typically renders product tiles with specific classes
        cards = soup.select(
            ".product-tile, .product, [data-product], "
            ".product-grid-item, .search-result-item"
        )

        for card in cards[:50]:
            scraped = self._parse_card(card)
            if scraped:
                products.append(scraped)

        # Try JSON-LD fallback
        if not products:
            products = self._extract_json_ld(soup)

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

        # HTML price elements
        for selector in [".product-price .sale", ".product-price", ".price-sales", ".price"]:
            el = soup.select_one(selector)
            if el:
                price = self.parse_price(el.get_text())
                if price:
                    return ScrapedPrice(price=price)

        return None

    def _parse_card(self, card) -> ScrapedProduct | None:
        link = card.find("a")
        if not link or not link.get("href"):
            return None

        href = link["href"]
        if not href.startswith("http"):
            href = f"{self.base_url}{href}"

        name_el = card.select_one(".product-name, .pdp-link a, h3, h4, [data-product-name]")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            return None

        price = None
        for sel in [".product-price .sale", ".product-price", ".price-sales", ".price"]:
            price_el = card.select_one(sel)
            if price_el:
                price = self.parse_price(price_el.get_text())
                if price:
                    break

        if price is None:
            return None

        img = card.find("img")
        image_url = ""
        if img:
            image_url = img.get("src") or img.get("data-src") or ""
            if image_url.startswith("//"):
                image_url = "https:" + image_url

        # Extract brand from product card
        brand_el = card.select_one(".product-brand, .brand, .brand-name")
        brand_name = brand_el.get_text(strip=True) if brand_el else ""

        return ScrapedProduct(
            name=name,
            url=href,
            price=price,
            image_url=image_url,
            thumbnail_url=image_url,
            brand=brand_name,
        )

    def _extract_json_ld(self, soup) -> list[ScrapedProduct]:
        products: list[ScrapedProduct] = []
        scripts = soup.find_all("script", {"type": "application/ld+json"})

        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for entry in data.get("itemListElement", []):
                        item = entry.get("item", entry)
                        if item.get("@type") == "Product":
                            name = item.get("name", "")
                            url = item.get("url", "")
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            price = self.parse_price(str(offers.get("price", "")))
                            brand_info = item.get("brand", {})
                            brand_name = ""
                            if isinstance(brand_info, dict):
                                brand_name = brand_info.get("name", "")
                            elif isinstance(brand_info, str):
                                brand_name = brand_info
                            if name and price:
                                products.append(ScrapedProduct(
                                    name=name,
                                    url=url if url.startswith("http") else f"{self.base_url}{url}",
                                    price=price,
                                    image_url=item.get("image", ""),
                                    brand=brand_name,
                                ))
            except (json.JSONDecodeError, AttributeError):
                continue

        return products
