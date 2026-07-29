"""Shared helpers for the Next.js + commercetools storefronts.

Altitude Sports and The Last Hunt are sister sites running the same stack, so
the product-detail payload is identical on both.

Pricing lives in the React Query cache embedded in __NEXT_DATA__:

    props.pageProps.dehydratedState.queries[].state.data.masterVariant.price
        .value.centAmount             -> list price
        .discounted.value.centAmount  -> sale price, when discounted

Note this is the *only* place the price appears. The JSON-LD block on these
pages is a ProductGroup whose hasVariant array is empty and which carries no
offers at all, so there is nothing to fall back to.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from src.retailers.base import ScrapedPrice

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_next_data(html: str) -> Optional[dict]:
    """Pull and parse the __NEXT_DATA__ blob out of raw page HTML.

    Must be given raw HTML — matching against BeautifulSoup's .text can never
    work, since that strips the very <script> tag being searched for.
    """
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _cent_amount(money: object) -> Optional[int]:
    if not isinstance(money, dict):
        return None
    value = money.get("value")
    if not isinstance(value, dict):
        return None
    cents = value.get("centAmount")
    return cents if isinstance(cents, int) else None


def price_from_next_data(next_data: dict) -> Optional[ScrapedPrice]:
    """Read the current price out of a product page's __NEXT_DATA__.

    Returns None when the payload carries no price. That happens on real,
    HTTP-200 pages for discontinued products, so it means "no price available"
    rather than "page is broken" — the caller decides what to do with that.
    """
    page_props = (next_data.get("props") or {}).get("pageProps") or {}
    queries = (page_props.get("dehydratedState") or {}).get("queries") or []

    for query in queries:
        data = (query.get("state") or {}).get("data")
        if not isinstance(data, dict):
            continue

        variant = data.get("masterVariant")
        if not isinstance(variant, dict):
            variants = data.get("variants")
            variant = variants[0] if isinstance(variants, list) and variants else None
        if not isinstance(variant, dict):
            continue

        price_obj = variant.get("price")
        if not isinstance(price_obj, dict):
            continue

        list_cents = _cent_amount(price_obj)
        sale_cents = _cent_amount(price_obj.get("discounted"))
        if list_cents is None and sale_cents is None:
            continue

        current = sale_cents if sale_cents is not None else list_cents
        on_sale = (
            sale_cents is not None
            and list_cents is not None
            and sale_cents < list_cents
        )
        currency = (price_obj.get("value") or {}).get("currencyCode") or "CAD"

        return ScrapedPrice(
            price=current,
            original_price=list_cents if on_sale else None,
            on_sale=on_sale,
            currency=currency,
            available=True,
        )

    return None
