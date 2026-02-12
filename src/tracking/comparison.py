"""Cross-retailer product comparison utilities."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import Brand, Product


def normalize_product_name(name: str, brand_name: str = "") -> str:
    """Normalize a product name for cross-retailer matching.

    Strips brand prefix, gender terms, color suffixes, and punctuation.
    """
    n = name.lower()

    # Remove brand name from start
    if brand_name:
        brand_lower = brand_name.lower()
        if n.startswith(brand_lower):
            n = n[len(brand_lower):].strip(" -")

    # Remove gender terms
    n = re.sub(r"\b(men'?s?|women'?s?|unisex|homme|femme)\b", "", n)

    # Remove color suffixes after a dash or slash
    n = re.sub(
        r"\s*[-/]\s*(black|white|grey|gray|navy|blue|red|green|brown|beige|tan|"
        r"olive|cream|sand|charcoal|khaki|pink|orange|purple|yellow|burgundy|"
        r"maroon|coral|teal|sage|noir|blanc|gris).*$",
        "", n,
    )

    # Remove size info
    n = re.sub(r"\s*[-/]\s*size.*$", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\s*[-/]\s*(xs|s|m|l|xl|xxl|\d+)$", "", n)

    # Strip punctuation and collapse whitespace
    n = re.sub(r"[^a-z0-9\s]", "", n)
    n = re.sub(r"\s+", " ", n).strip()

    return n


async def find_similar_products(
    session: AsyncSession,
    product: Product,
    threshold: float = 0.75,
) -> List[Product]:
    """Find the same product at other retailers by fuzzy name matching."""
    if not product.brand_id:
        return []

    result = await session.execute(
        select(Product)
        .where(
            Product.brand_id == product.brand_id,
            Product.retailer_id != product.retailer_id,
            Product.current_price.isnot(None),
        )
        .options(selectinload(Product.retailer))
    )
    candidates = result.scalars().all()

    if not candidates:
        return []

    brand_name = ""
    if product.brand:
        brand_name = product.brand.name
    else:
        brand_result = await session.get(Brand, product.brand_id)
        brand_name = brand_result.name if brand_result else ""

    norm = normalize_product_name(product.name, brand_name)

    matches = []
    for c in candidates:
        c_norm = normalize_product_name(c.name, brand_name)
        ratio = SequenceMatcher(None, norm, c_norm).ratio()
        if ratio >= threshold:
            matches.append((c, ratio))

    # Sort by price ascending
    matches.sort(key=lambda x: x[0].current_price or 999999)
    return [m[0] for m in matches]


def compute_cheapest_ids(
    products: list,
    brand_name: str = "",
) -> set:
    """Given a list of products, group by normalized name and return IDs of the cheapest
    in each group that has multiple retailers."""
    name_groups: dict[str, list] = {}
    for p in products:
        norm = normalize_product_name(p.name, brand_name)
        if norm not in name_groups:
            name_groups[norm] = []
        name_groups[norm].append(p)

    cheapest_ids = set()
    for group in name_groups.values():
        if len(group) > 1:
            # Check they're from different retailers
            retailer_ids = {p.retailer_id for p in group}
            if len(retailer_ids) > 1:
                cheapest = min(group, key=lambda p: p.current_price or 999999)
                cheapest_ids.add(cheapest.id)

    return cheapest_ids
