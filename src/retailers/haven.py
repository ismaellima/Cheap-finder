"""Haven (havenshop.com) scraper.

Platform: Shopify Hydrogen (headless). JSON API endpoints return empty.
Must parse HTML from collection pages and extract JSON-LD structured data.
"""
from __future__ import annotations

import json
import logging
import re

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class HavenScraper(RetailerBase):
    name = "Haven"
    slug = "haven"
    base_url = "https://havenshop.com"
    requires_js = False  # JSON-LD is in initial HTML

    brand_slug_map = {
        "arc'teryx": "arcteryx",
        "arcteryx": "arcteryx",
        "new balance": "new-balance",
        "satisfy": "satisfy",
        "satisfy running": "satisfy",
    }

    def _brand_to_slug(self, brand_name: str) -> str:
        lower = brand_name.lower()
        for key, slug in self.brand_slug_map.items():
            if key in lower or lower in key:
                return slug
        return re.sub(r"[^a-z0-9]+", "-", lower).strip("-")

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        slug = self._brand_to_slug(brand_name)
        url = f"{self.base_url}/collections/{slug}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.warning(f"{self.name}: Failed to fetch collection page for '{brand_name}'")
            return []

        products: list[ScrapedProduct] = []

        # Try JSON-LD ItemList first
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") == "ItemList":
                            data = item
                            break

                if data.get("@type") == "ItemList":
                    for item_entry in data.get("itemListElement", []):
                        product_data = item_entry.get("item", item_entry)
                        scraped = self._parse_json_ld_product(product_data)
                        if scraped:
                            products.append(scraped)
            except (json.JSONDecodeError, AttributeError):
                continue

        if products:
            logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}' via JSON-LD")
            return products

        # Fallback: try parsing Remix context
        products = self._parse_remix_context(soup)
        if products:
            logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}' via Remix context")

        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        try:
            soup = await self._fetch_soup(product_url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch {product_url}")
            return None

        # Try JSON-LD Product
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
                    if price is not None:
                        return ScrapedPrice(
                            price=price,
                            currency=offers.get("priceCurrency", "CAD"),
                            available=offers.get("availability", "").endswith("InStock"),
                        )
            except (json.JSONDecodeError, AttributeError):
                continue

        # Fallback: meta tag
        meta = soup.find("meta", {"property": "product:price:amount"})
        if meta and meta.get("content"):
            price = self.parse_price(meta["content"])
            if price:
                return ScrapedPrice(price=price)

        return None

    def _parse_json_ld_product(self, data: dict) -> ScrapedProduct | None:
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
            sku=data.get("sku", ""),
        )

    def _parse_remix_context(self, soup) -> list[ScrapedProduct]:
        """Try to extract products from Shopify Hydrogen's Remix context."""
        products: list[ScrapedProduct] = []

        for script in soup.find_all("script"):
            text = script.string or ""
            if "window.__remixContext" not in text:
                continue

            # Extract JSON from the script using brace balancing
            match = re.search(r"window\.__remixContext\s*=\s*(\{.*)", text, re.DOTALL)
            if not match:
                continue

            try:
                raw = match.group(1)
                depth = 0
                end = 0
                for i, ch in enumerate(raw):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                context = json.loads(raw[:end])
                # Navigate to products in the loader data
                loader_data = context.get("state", {}).get("loaderData", {})
                for key, value in loader_data.items():
                    if isinstance(value, dict) and "collection" in value:
                        collection = value["collection"]
                        nodes = (
                            collection.get("products", {}).get("nodes", [])
                        )
                        for node in nodes:
                            scraped = self._parse_hydrogen_node(node)
                            if scraped:
                                products.append(scraped)
            except (json.JSONDecodeError, AttributeError, KeyError):
                continue

        return products

    def _parse_hydrogen_node(self, node: dict) -> ScrapedProduct | None:
        title = node.get("title", "")
        handle = node.get("handle", "")
        if not title or not handle:
            return None

        price_range = node.get("priceRange", {})
        min_price = price_range.get("minVariantPrice", {})
        price = self.parse_price(str(min_price.get("amount", "")))
        if price is None:
            return None

        compare_range = node.get("compareAtPriceRange", {})
        compare_price_data = compare_range.get("minVariantPrice", {})
        compare_price = self.parse_price(str(compare_price_data.get("amount", "")))
        on_sale = compare_price is not None and compare_price > price

        image = node.get("featuredImage", {}).get("url", "")

        return ScrapedProduct(
            name=title,
            url=f"{self.base_url}/products/{handle}",
            price=price,
            original_price=compare_price if on_sale else None,
            on_sale=on_sale,
            image_url=image,
            thumbnail_url=image,
        )
