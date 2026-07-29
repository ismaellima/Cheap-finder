"""Tests for the Shopify JSON endpoint URL construction.

Discovery stores product links scraped from Shopify search results, which
carry tracking params (?_pos=&_psq=&_ss=&_v=). Appending ".json" to the end
of the whole URL puts it on the query string instead of the path; Shopify
answers that with the ordinary HTML page, which then fails to parse as JSON
and is indistinguishable from an unreadable product.
"""
from __future__ import annotations

import pytest

from src.retailers.shopify_base import ShopifyBase

to_json = ShopifyBase._to_json_endpoint


def test_search_tracking_params_are_dropped_and_json_lands_on_the_path():
    url = "https://www.deadstock.ca/products/arcteryx-kragg?_pos=4&_psq=Arc&_ss=e&_v=1.0"
    assert to_json(url) == "https://www.deadstock.ca/products/arcteryx-kragg.json"


def test_plain_product_url_gets_json_suffix():
    assert (
        to_json("https://havenshop.com/products/beams-tee")
        == "https://havenshop.com/products/beams-tee.json"
    )


def test_trailing_slash_does_not_produce_a_double_segment():
    assert (
        to_json("https://shop.example/products/thing/")
        == "https://shop.example/products/thing.json"
    )


def test_already_json_url_is_left_alone():
    assert (
        to_json("https://shop.example/products/thing.json")
        == "https://shop.example/products/thing.json"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example/products/a?_pos=1",
        "https://shop.example/products/a/?_pos=1&_v=1.0",
        "https://shop.example/products/a",
    ],
)
def test_result_is_always_a_bare_json_path(url):
    result = to_json(url)
    assert result.endswith(".json")
    assert "?" not in result, "query string must not survive onto the JSON endpoint"
    assert ".json" not in result[: result.rfind(".json")], "only one .json suffix"
