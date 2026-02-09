"""NRML (nrml.ca) scraper.

Platform: Shopify Classic with full JSON API access.
Brands carried: On Cloud, New Balance, and other streetwear/sneaker brands.
"""
from __future__ import annotations

from src.retailers.shopify_base import ShopifyBase


class NRMLScraper(ShopifyBase):
    name = "NRML"
    slug = "nrml"
    base_url = "https://nrml.ca"
    requires_js = False

    brand_slug_map = {
        "on cloud": "on-cloud",
        "on running": "on-cloud",
        "new balance": "new-balance",
        "a.p.c.": "apc",
        "apc": "apc",
        "arc'teryx": "arcteryx",
        "arcteryx": "arcteryx",
        "satisfy": "satisfy",
        "sabre": "sabre",
    }
