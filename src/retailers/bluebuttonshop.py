"""Scraper for Blue Button Shop (bluebuttonshop.com).

Blue Button Shop uses a custom PHP platform (not Shopify).
URL patterns:
  - /shop/BRAND/{M|W|D}/{Brand-Name}/ALL/0 — brand page
  - /shop/SEARCH/{M|W|D}/{query}/ALL/0 — search
  - /PDETAILS/{M|W|D}/{id}/{slug} — product detail

Product cards use `<DIV class='css-prod-frame'>` with:
  - css-image: product link + images
  - css-desc: brand name + product name
  - css-price: price, with css-strike-through for original + red span for sale
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from src.retailers.base import RetailerBase, ScrapedPrice, ScrapedProduct

logger = logging.getLogger(__name__)


class BlueButtonShopScraper(RetailerBase):
    """Scraper for Blue Button Shop (custom PHP platform)."""

    name = "Blue Button Shop"
    slug = "bluebuttonshop"
    base_url = "https://www.bluebuttonshop.com"
    requires_js = False

    # Map known brand names to BBS URL slugs
    brand_slug_map: dict[str, str] = {
        "new balance": "New-Balance",
        "arc'teryx": "Arcteryx",
        "arcteryx": "Arcteryx",
        "a.p.c.": "APC",
        "apc": "APC",
        "apfr": "APFR",
        "on cloud": "On",
        "on running": "On",
        "beams plus": "Beams-Plus",
        "satisfy": "Satisfy",
        "satisfy running": "Satisfy",
        "goldwin": "Goldwin",
        "goldwin 0": "Goldwin-0",
        "balmoral": "Balmoral",
    }

    def _brand_to_url_slug(self, brand_name: str) -> str:
        """Convert a brand name to BBS URL slug format."""
        lower = brand_name.lower()
        for key, slug in self.brand_slug_map.items():
            if key in lower or lower in key:
                return slug
        # Fallback: capitalize words and join with hyphens
        return "-".join(w.capitalize() for w in brand_name.split())

    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        """Search for products by brand using the brand listing page."""
        products: list[ScrapedProduct] = []

        slug = self._brand_to_url_slug(brand_name)

        # Try brand page (D = all genders)
        brand_url = f"{self.base_url}/shop/BRAND/D/{slug}/ALL/0"
        try:
            soup = await self._fetch_soup(brand_url)
            products = self._parse_product_listing(soup, brand_name)
            if products:
                logger.info(
                    f"{self.name}: Found {len(products)} products for "
                    f"'{brand_name}' via brand page"
                )
                return products
        except Exception:
            logger.exception(
                f"{self.name}: Failed to fetch brand page for '{brand_name}'"
            )

        # Fallback: try search
        search_url = f"{self.base_url}/shop/SEARCH/D/{slug}/ALL/0"
        try:
            soup = await self._fetch_soup(search_url)
            products = self._parse_product_listing(soup, brand_name)
            if products:
                logger.info(
                    f"{self.name}: Found {len(products)} products for "
                    f"'{brand_name}' via search"
                )
        except Exception:
            logger.exception(
                f"{self.name}: Failed to search for '{brand_name}'"
            )

        return products

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        """Get price for a specific product URL."""
        url = product_url
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"

        try:
            soup = await self._fetch_soup(url)
        except Exception:
            logger.exception(f"{self.name}: Failed to fetch product {url}")
            return None

        # Product detail pages use css-price-line, listing pages use css-price
        price_div = soup.find("div", class_="css-price-line")
        if not price_div:
            price_div = soup.find("div", class_="css-price")

        if not price_div:
            return None

        return self._parse_price_div(price_div)

    def _parse_product_listing(
        self, soup: BeautifulSoup, brand_name: str
    ) -> list[ScrapedProduct]:
        """Parse product cards from a listing page."""
        products: list[ScrapedProduct] = []
        frames = soup.find_all("div", class_="css-prod-frame")

        for frame in frames:
            product = self._parse_product_card(frame, brand_name)
            if product:
                products.append(product)

        return products

    def _parse_product_card(
        self, frame: Tag, brand_name: str
    ) -> ScrapedProduct | None:
        """Parse a single css-prod-frame into a ScrapedProduct."""
        # Get product URL from the image link
        image_div = frame.find("div", class_="css-image")
        if not image_div:
            return None

        link = image_div.find("a")
        if not link or not link.get("href"):
            return None

        product_url = link["href"]
        if not product_url.startswith("http"):
            product_url = f"{self.base_url}{product_url}"

        # Get images
        images = link.find_all("img")
        image_url = ""
        thumbnail_url = ""
        for img in images:
            src = img.get("src", "")
            if src:
                full_src = src if src.startswith("http") else f"{self.base_url}{src}"
                if not image_url:
                    image_url = full_src
                thumbnail_url = full_src

        # Get brand and product name from css-desc
        desc_div = frame.find("div", class_="css-desc")
        if not desc_div:
            return None

        brand_span = desc_div.find("span")
        brand_text = brand_span.get_text(strip=True) if brand_span else ""

        # Product name is the text after <BR>
        desc_text = desc_div.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in desc_text.split("\n") if l.strip()]
        product_name = lines[-1] if len(lines) > 1 else lines[0] if lines else ""

        if not product_name:
            return None

        # Parse price from css-price div
        price_div = frame.find("div", class_="css-price")
        if not price_div:
            return None

        price_info = self._parse_price_div(price_div)
        if not price_info:
            return None

        # Detect gender from URL
        gender = ""
        if "/M/" in link["href"]:
            gender = "men"
        elif "/W/" in link["href"]:
            gender = "women"

        return ScrapedProduct(
            name=product_name,
            url=product_url,
            price=price_info.price,
            original_price=price_info.original_price,
            on_sale=price_info.on_sale,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            brand=brand_text,
            gender=gender,
        )

    def _parse_price_div(self, price_div: Tag) -> ScrapedPrice | None:
        """Parse a css-price div to extract current and original prices.

        Regular item: <SPAN>160.00</SPAN>
        Sale item: <SPAN class="css-strike-through">319.00</SPAN>
                   <SPAN style='color:red'>207.35</SPAN>
        """
        spans = price_div.find_all("span")
        if not spans:
            return None

        strike_span = price_div.find("span", class_="css-strike-through")

        if strike_span:
            # Sale item
            original_price = self.parse_price(strike_span.get_text(strip=True))
            # Sale price is the span with color:red
            sale_span = price_div.find("span", style=re.compile(r"color:\s*red"))
            if sale_span:
                sale_price = self.parse_price(sale_span.get_text(strip=True))
            else:
                sale_price = None

            if sale_price is not None:
                return ScrapedPrice(
                    price=sale_price,
                    original_price=original_price,
                    on_sale=True,
                    currency="CAD",
                )
        else:
            # Regular price — find the span with a numeric value
            for span in spans:
                text = span.get_text(strip=True)
                price = self.parse_price(text)
                if price is not None and price > 0:
                    return ScrapedPrice(
                        price=price,
                        original_price=None,
                        on_sale=False,
                        currency="CAD",
                    )

        return None
