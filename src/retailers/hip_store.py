"""The Hip Store (thehipstore.co.uk) scraper.

UK-based retailer that ships to Canada. Uses HTML + JSON-LD parsing.
Prices in GBP — we note this but store as-is (conversion can be added later).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class HipStoreScraper(RetailerBase):
    name = "The Hip Store"
    slug = "hip_store"
    base_url = "https://www.thehipstore.co.uk"
    requires_js = False

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        encoded = urllib.parse.quote(brand_name)
        url = f"{self.base_url}/search/{encoded}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.warning(f"{self.name}: Failed to search for '{brand_name}'")
            return []

        products: list[ScrapedProduct] = []

        # Try product cards
        cards = soup.select(
            ".productListItem, .product-card, .product-item, "
            "[data-product], article.product"
        )
        for card in cards[:50]:
            link = card.find("a")
            if not link or not link.get("href"):
                continue

            href = link["href"]
            if not href.startswith("http"):
                href = f"{self.base_url}{href}"

            name_el = card.select_one("h3, h4, .productTitle, .product-name, [data-product-name]")
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue

            price = None
            for sel in [".price, .productPrice, [data-price]"]:
                el = card.select_one(sel)
                if el:
                    price = self.parse_price(el.get_text())
                    if price:
                        break

            if price is None:
                continue

            img = card.find("img")
            image_url = ""
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                if image_url.startswith("//"):
                    image_url = "https:" + image_url

            products.append(ScrapedProduct(
                name=name, url=href, price=price,
                image_url=image_url, thumbnail_url=image_url,
            ))

        # JSON-LD fallback
        if not products:
            scripts = soup.find_all("script", {"type": "application/ld+json"})
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get("@type") == "ItemList":
                        for entry in data.get("itemListElement", []):
                            item = entry.get("item", entry)
                            name = item.get("name", "")
                            url = item.get("url", "")
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            price = self.parse_price(str(offers.get("price", "")))
                            if name and price:
                                products.append(ScrapedProduct(
                                    name=name,
                                    url=url if url.startswith("http") else f"{self.base_url}{url}",
                                    price=price,
                                    image_url=item.get("image", ""),
                                ))
                except (json.JSONDecodeError, AttributeError):
                    continue

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
                            currency=offers.get("priceCurrency", "GBP"),
                            available="InStock" in str(offers.get("availability", "")),
                        )
            except (json.JSONDecodeError, AttributeError):
                continue

        # Meta tags
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            price = self.parse_price(meta["content"])
            if price:
                return ScrapedPrice(price=price, currency="GBP")

        return None
