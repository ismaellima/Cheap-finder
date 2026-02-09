"""SSENSE (ssense.com) scraper.

Platform: Custom, JS-heavy. Product data is embedded in page source as JSON.
Brand pages: /en-ca/{gender}/designers/{brand-slug}
This scraper parses the initial HTML which contains product data in script tags.
Falls back to Playwright if static fetching fails.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class SSENSEScraper(RetailerBase):
    name = "SSENSE"
    slug = "ssense"
    base_url = "https://www.ssense.com"
    requires_js = True

    brand_slug_map = {
        "arc'teryx": "arcteryx",
        "arcteryx": "arcteryx",
        "a.p.c.": "apc",
        "apc": "apc",
        "new balance": "new-balance",
        "on cloud": "on",
        "on running": "on",
        "satisfy": "satisfy",
        "satisfy running": "satisfy",
        "sabre paris": "sabre",
        "sabre": "sabre",
        "balmoral": "balmoral",
    }

    def _brand_to_slug(self, brand_name: str) -> str:
        lower = brand_name.lower()
        for key, slug in self.brand_slug_map.items():
            if key in lower or lower in key:
                return slug
        return re.sub(r"[^a-z0-9]+", "-", lower).strip("-")

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        slug = self._brand_to_slug(brand_name)
        products: list[ScrapedProduct] = []

        # Try both men and women pages
        for gender in ["men", "women"]:
            url = f"{self.base_url}/en-ca/{gender}/designers/{slug}"
            try:
                html = await self._fetch(url)
            except Exception:
                logger.debug(f"{self.name}: Failed to fetch {gender} page for '{brand_name}'")
                continue

            page_products = self._extract_products_from_html(html, gender)
            products.extend(page_products)

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

    def _extract_products_from_html(self, html: str, gender: str) -> list[ScrapedProduct]:
        """Extract product data from SSENSE page HTML."""
        products: list[ScrapedProduct] = []

        # SSENSE embeds product data in __NEXT_DATA__ or similar script tags
        patterns = [
            r'__NEXT_DATA__\s*=\s*(\{.+?\})\s*;?\s*</script>',
            r'"products"\s*:\s*(\[.+?\])\s*[,}]',
            r'window\.__PRELOADED_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    extracted = self._parse_next_data(data, gender)
                    if extracted:
                        return extracted
                except (json.JSONDecodeError, KeyError):
                    continue

        # Fallback: parse JSON-LD from HTML
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for entry in data.get("itemListElement", []):
                        item = entry.get("item", entry)
                        if item.get("@type") == "Product":
                            p = self._json_ld_to_product(item, gender)
                            if p:
                                products.append(p)
            except (json.JSONDecodeError, AttributeError):
                continue

        return products

    def _parse_next_data(self, data: dict, gender: str) -> list[ScrapedProduct]:
        """Parse __NEXT_DATA__ structure for products."""
        products: list[ScrapedProduct] = []

        # Navigate through common Next.js data structures
        page_props = data.get("props", {}).get("pageProps", {})
        product_list = (
            page_props.get("products")
            or page_props.get("initialProducts")
            or page_props.get("productListing", {}).get("products")
            or []
        )

        for p in product_list:
            name = p.get("name") or p.get("title", "")
            slug = p.get("slug") or p.get("handle", "")
            brand = p.get("brand", {})
            brand_name = brand.get("name", "") if isinstance(brand, dict) else str(brand)

            price_data = p.get("price", {})
            if isinstance(price_data, dict):
                price = self.parse_price(str(price_data.get("regular", "")))
                sale_price = self.parse_price(str(price_data.get("sale", "") or ""))
            else:
                price = self.parse_price(str(price_data))
                sale_price = None

            if price is None:
                continue

            on_sale = sale_price is not None and sale_price < price
            current_price = sale_price if on_sale else price

            image_url = ""
            images = p.get("images", []) or p.get("image", [])
            if images:
                if isinstance(images[0], dict):
                    image_url = images[0].get("url", "") or images[0].get("src", "")
                elif isinstance(images[0], str):
                    image_url = images[0]
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url

            url = f"{self.base_url}/en-ca/{gender}/{slug}" if slug else ""

            display_name = f"{brand_name} {name}".strip() if brand_name else name

            products.append(ScrapedProduct(
                name=display_name,
                url=url,
                price=current_price,
                original_price=price if on_sale else None,
                on_sale=on_sale,
                image_url=image_url,
                thumbnail_url=image_url,
                gender=gender,
            ))

        return products

    def _json_ld_to_product(self, data: dict, gender: str) -> ScrapedProduct | None:
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

        return ScrapedProduct(
            name=name,
            url=url,
            price=price,
            image_url=image,
            thumbnail_url=image,
            gender=gender,
        )
