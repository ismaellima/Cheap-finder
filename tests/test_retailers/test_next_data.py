"""Tests for the Next.js/commercetools price parser.

Payload shapes are copied from live Altitude Sports and The Last Hunt product
pages (2026-07-29).
"""
from __future__ import annotations

from src.retailers.next_data import extract_next_data, price_from_next_data


def _payload(price_obj):
    return {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        # Real pages carry several unrelated queries first.
                        {"state": {"data": {"unrelated": True}}},
                        {"state": {"data": None}},
                        {"state": {"data": {"masterVariant": {"price": price_obj}}}},
                    ]
                }
            }
        }
    }


def test_reads_plain_list_price():
    payload = _payload(
        {"value": {"centAmount": 18499, "currencyCode": "CAD"}, "discounted": None}
    )
    price = price_from_next_data(payload)
    assert price is not None
    assert price.price == 18499
    assert price.on_sale is False
    assert price.original_price is None
    assert price.currency == "CAD"


def test_discounted_price_wins_and_keeps_original():
    payload = _payload(
        {
            "value": {"centAmount": 23999, "currencyCode": "CAD"},
            "discounted": {"value": {"centAmount": 20399}},
        }
    )
    price = price_from_next_data(payload)
    assert price.price == 20399, "should report what the customer actually pays"
    assert price.original_price == 23999
    assert price.on_sale is True


def test_discontinued_product_yields_no_price():
    """Live 200-response pages for delisted products carry price: null."""
    assert price_from_next_data(_payload(None)) is None


def test_falls_back_to_first_variant_when_no_master():
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "variants": [
                                        {"price": {"value": {"centAmount": 7000}}}
                                    ]
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    assert price_from_next_data(payload).price == 7000


def test_empty_payload_is_handled():
    assert price_from_next_data({}) is None
    assert price_from_next_data({"props": {"pageProps": {}}}) is None


def test_extract_next_data_needs_raw_html_not_stripped_text():
    html = '<html><body><script id="__NEXT_DATA__">{"a": 1}</script></body></html>'
    assert extract_next_data(html) == {"a": 1}
    # The old code searched BeautifulSoup's .text, which drops the script tag
    # entirely, so the pattern could never match.
    assert extract_next_data("{'a': 1}") is None


def test_malformed_json_does_not_raise():
    assert extract_next_data('<script id="__NEXT_DATA__">{not json</script>') is None
