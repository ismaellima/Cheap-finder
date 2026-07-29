"""The Last Hunt (thelasthunt.com) scraper.

Platform: Next.js SPA with Algolia search — sister site of Altitude Sports.
Product data is embedded in __NEXT_DATA__ within serverState.initialResults
(Algolia InstantSearch, index: PRODUCTS_TLH_en-CA).

Brand pages: /c/{brand-slug}  (slug = lowercase, spaces → hyphens)
Product pages: /p/{product-slug}
Brands list: /brands (returns {name, slug} pairs in pageProps)

Pagination: 48 products per page, ?page=N (1-indexed in URL).
All products are outlet/clearance so most have discounted prices.
"""
from __future__ import annotations

import json
import logging
import re

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct
from src.retailers.next_data import extract_next_data, price_from_next_data

logger = logging.getLogger(__name__)


def _brand_to_slug(brand_name: str) -> str:
    """Convert a brand name to a URL slug.

    "New Balance" → "new-balance", "Arc'teryx" → "arcteryx"
    """
    slug = brand_name.lower()
    # Remove apostrophes and periods before slugifying
    slug = slug.replace("'", "").replace(".", "").replace("'", "")
    # Replace spaces and underscores with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)
    # Remove anything that isn't alphanumeric or hyphen
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    # Collapse multiple hyphens
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


class TheLastHuntScraper(RetailerBase):
    name = "The Last Hunt"
    slug = "the_last_hunt"
    base_url = "https://www.thelasthunt.com"
    requires_js = False  # __NEXT_DATA__ is in initial HTML

    # Known slug overrides for brand names that don't follow the simple pattern
    _SLUG_OVERRIDES: dict[str, str] = {
        "on running": "on",
        "on cloud": "on",
    }

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        slug = self._SLUG_OVERRIDES.get(brand_name.lower(), _brand_to_slug(brand_name))
        all_products: list[ScrapedProduct] = []

        # Fetch page 1
        products, nb_pages = await self._fetch_brand_page(slug, page=1)
        all_products.extend(products)

        # Fetch remaining pages (if any)
        for page_num in range(2, nb_pages + 1):
            page_products, _ = await self._fetch_brand_page(slug, page=page_num)
            all_products.extend(page_products)

        logger.info(
            f"{self.name}: Found {len(all_products)} products for "
            f"'{brand_name}' (slug={slug}, {nb_pages} pages)"
        )
        return all_products

    async def _fetch_brand_page(
        self, slug: str, page: int = 1,
    ) -> tuple[list[ScrapedProduct], int]:
        """Fetch a single brand page and return (products, total_pages)."""
        url = f"{self.base_url}/c/{slug}"
        if page > 1:
            url += f"?page={page}"

        try:
            html = await self._fetch(url)
        except Exception:
            logger.warning(
                f"{self.name}: Failed to fetch brand page /c/{slug} (page {page})"
            )
            return [], 0

        return self._extract_from_next_data(html)

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        try:
            html = await self._fetch(product_url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch {product_url}")
            return None

        next_data = extract_next_data(html)
        if next_data is None:
            logger.warning(f"{self.name}: No __NEXT_DATA__ on {product_url}")
            return None

        return price_from_next_data(next_data)

    def _extract_from_next_data(
        self, html: str,
    ) -> tuple[list[ScrapedProduct], int]:
        """Extract products from __NEXT_DATA__ Algolia search results.

        Returns (products, total_pages).
        """
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if not match:
            return [], 0

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            return [], 0

        page_props = data.get("props", {}).get("pageProps", {})
        server_state = page_props.get("serverState", {})
        initial_results = server_state.get("initialResults", {})

        products: list[ScrapedProduct] = []
        nb_pages = 0

        for key, value in initial_results.items():
            if "PRODUCTS" not in key:
                continue
            if not isinstance(value, dict):
                continue
            results = value.get("results", [])
            for result_group in results:
                if not isinstance(result_group, dict):
                    continue
                nb_pages = max(nb_pages, result_group.get("nbPages", 0))
                for hit in result_group.get("hits", []):
                    p = self._parse_algolia_hit(hit)
                    if p:
                        products.append(p)

        return products, nb_pages

    @staticmethod
    def _extract_cents(price_obj) -> int | None:
        """Extract price in cents from price structure.

        Format: {"CAD": {"centAmount": [12999]}} or {"CAD": {"centAmount": 12999}}
        """
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

        url = f"{self.base_url}/p/{slug}"

        # Price: {"CAD": {"centAmount": [10289]}}
        price = self._extract_cents(hit.get("price", {}))
        if price is None:
            return None

        # Original price: {"CAD": {"centAmount": 20999}} (note: not always a list)
        original_price = self._extract_cents(hit.get("original_price", {}))

        on_sale = original_price is not None and original_price > price
        discount = hit.get("discounted_percent", 0)
        if isinstance(discount, list):
            discount = discount[0] if discount else 0
        if discount and isinstance(discount, (int, float)) and discount > 0:
            on_sale = True

        image_url = hit.get("image_url", "")

        # Thumbnails: list of {id, color_name, image_url, price, discounted_percent}
        thumbnails = hit.get("thumbnails", [])
        thumbnail_url = image_url
        if isinstance(thumbnails, list) and thumbnails:
            first = thumbnails[0]
            if isinstance(first, dict):
                thumbnail_url = first.get("image_url", image_url)
            elif isinstance(first, str):
                thumbnail_url = first

        # Brand name from attributes
        attributes = hit.get("attributes", {})
        brand_name = ""
        if isinstance(attributes, dict):
            brand_name = attributes.get("brand_name", "")

        # Gender from attributes (tag list or explicit field)
        gender = ""
        if isinstance(attributes, dict):
            gender_val = attributes.get("gender", "")
            if isinstance(gender_val, str):
                gender = gender_val.lower()

        # Infer gender from product name if not in attributes
        if not gender:
            name_lower = name.lower()
            if "women" in name_lower or "- women's" in name_lower:
                gender = "women"
            elif " men's" in name_lower or "- men's" in name_lower:
                gender = "men"

        return ScrapedProduct(
            name=name,
            url=url,
            price=price,
            original_price=original_price if on_sale else None,
            on_sale=on_sale,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            sku=hit.get("objectID", ""),
            brand=brand_name,
            gender=gender,
        )
