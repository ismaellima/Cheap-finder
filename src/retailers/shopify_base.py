"""Base scraper for Shopify-powered retailers.

Uses Shopify's JSON API endpoints:
  - /products.json?limit=250&page=N — all products
  - /collections/{handle}/products.json — brand-scoped
  - /products/{handle}.json — single product
  - /search/suggest.json — predictive search
"""
from __future__ import annotations

import logging
import re

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class ShopifyBase(RetailerBase):
    """Base scraper for classic Shopify stores with JSON API access."""

    # Subclasses should set brand_slug_map for known brand → collection handle mappings
    brand_slug_map: dict[str, str] = {}

    def _brand_to_slug(self, brand_name: str) -> str:
        """Convert a brand name to a Shopify collection handle."""
        lower = brand_name.lower()
        # Check explicit mapping first
        for key, slug in self.brand_slug_map.items():
            if key.lower() in lower or lower in key.lower():
                return slug
        # Fallback: slugify the name
        slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")
        return slug

    async def _fetch_json(self, url: str) -> dict | list | None:
        """Fetch a URL and parse as JSON."""
        import json

        try:
            text = await self._fetch(url)
            return json.loads(text)
        except Exception:
            logger.exception(f"Failed to fetch JSON from {url}")
            return None

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        """Search for products by brand using collection endpoint or search API."""
        products: list[ScrapedProduct] = []

        # Try collection endpoint first
        slug = self._brand_to_slug(brand_name)
        collection_url = f"{self.base_url}/collections/{slug}/products.json?limit=250"
        data = await self._fetch_json(collection_url)

        if data and isinstance(data, dict) and data.get("products"):
            for p in data["products"]:
                scraped = self._parse_shopify_product(p)
                if scraped:
                    products.append(scraped)
            logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}' via collection")
            return products

        # Fallback: try predictive search
        search_url = (
            f"{self.base_url}/search/suggest.json"
            f"?q={brand_name}&resources[type]=product&resources[limit]=20"
        )
        data = await self._fetch_json(search_url)
        if data and isinstance(data, dict):
            resources = data.get("resources", {}).get("results", {}).get("products", [])
            for p in resources:
                scraped = self._parse_search_product(p)
                if scraped:
                    products.append(scraped)
            logger.info(f"{self.name}: Found {len(products)} products for '{brand_name}' via search")

        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        """Get price for a specific product URL using the .json endpoint."""
        # Convert product URL to JSON endpoint
        json_url = product_url.rstrip("/")
        if not json_url.endswith(".json"):
            json_url += ".json"

        data = await self._fetch_json(json_url)
        if not data or not isinstance(data, dict):
            return None

        product = data.get("product", data)
        variants = product.get("variants", [])
        if not variants:
            return None

        variant = variants[0]
        price = self.parse_price(str(variant.get("price", "")))
        compare_price = self.parse_price(str(variant.get("compare_at_price", "") or ""))
        available = variant.get("available", True)

        if price is None:
            return None

        on_sale = compare_price is not None and compare_price > price

        return ScrapedPrice(
            price=price,
            original_price=compare_price if on_sale else None,
            on_sale=on_sale,
            currency="CAD",
            available=available,
        )

    def _parse_shopify_product(self, product: dict) -> ScrapedProduct | None:
        """Parse a Shopify product JSON object into a ScrapedProduct."""
        title = product.get("title", "")
        handle = product.get("handle", "")
        if not title or not handle:
            return None

        variants = product.get("variants", [])
        if not variants:
            return None

        variant = variants[0]
        price = self.parse_price(str(variant.get("price", "")))
        if price is None:
            return None

        compare_price = self.parse_price(str(variant.get("compare_at_price", "") or ""))
        on_sale = compare_price is not None and compare_price > price

        images = product.get("images", [])
        image_url = images[0].get("src", "") if images else ""
        # Create smaller thumbnail
        thumbnail_url = image_url
        if image_url and ".jpg" in image_url:
            thumbnail_url = re.sub(r"\.jpg", "_400x.jpg", image_url, count=1)
        elif image_url and ".png" in image_url:
            thumbnail_url = re.sub(r"\.png", "_400x.png", image_url, count=1)

        # Try to detect gender from tags or product type
        tags = product.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        gender = self._detect_gender(tags, title, product.get("product_type", ""))

        return ScrapedProduct(
            name=title,
            url=f"{self.base_url}/products/{handle}",
            price=price,
            original_price=compare_price if on_sale else None,
            on_sale=on_sale,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            sku=str(variant.get("sku", "")),
            gender=gender,
            brand=product.get("vendor", ""),
        )

    def _parse_search_product(self, product: dict) -> ScrapedProduct | None:
        """Parse a product from Shopify predictive search results."""
        title = product.get("title", "")
        url = product.get("url", "")
        if not title or not url:
            return None

        price = self.parse_price(str(product.get("price", "")))
        if price is None:
            return None

        compare_price = self.parse_price(str(product.get("compare_at_price_max", "") or ""))
        on_sale = compare_price is not None and compare_price > price

        image = product.get("image", "")
        if image and image.startswith("//"):
            image = "https:" + image

        return ScrapedProduct(
            name=title,
            url=f"{self.base_url}{url}" if not url.startswith("http") else url,
            price=price,
            original_price=compare_price if on_sale else None,
            on_sale=on_sale,
            image_url=image,
            thumbnail_url=image,
            brand=product.get("vendor", ""),
        )

    @staticmethod
    def _detect_gender(tags: list[str], title: str, product_type: str) -> str:
        """Detect gender from tags, title, or product type."""
        all_text = " ".join(tags + [title, product_type]).lower()

        men_keywords = ["men's", "mens", "male", "homme", "man "]
        women_keywords = ["women's", "womens", "female", "femme", "woman "]
        unisex_keywords = ["unisex", "gender neutral", "gender-neutral"]

        for kw in unisex_keywords:
            if kw in all_text:
                return "unisex"

        is_men = any(kw in all_text for kw in men_keywords)
        is_women = any(kw in all_text for kw in women_keywords)

        if is_men and is_women:
            return "unisex"
        if is_men:
            return "men"
        if is_women:
            return "women"

        return ""
