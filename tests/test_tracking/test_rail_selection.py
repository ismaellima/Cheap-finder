"""Tests for the dashboard drops rail line-up.

The rail is fed candidates already sorted best-discount-first; its job is only
to stop any one brand or retailer taking the whole thing.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.api.routes_dashboard import (
    RAIL_DEALS_PER_BRAND,
    RAIL_DEALS_PER_RETAILER,
    RAIL_DEALS_TOTAL,
    select_rail_drops,
)


@dataclass
class FakeProduct:
    id: int
    brand_id: int
    retailer_id: int


def test_respects_total_cap():
    # 40 products, all distinct brands and retailers, so only the total binds.
    products = [FakeProduct(i, brand_id=i, retailer_id=i) for i in range(40)]
    assert len(select_rail_drops(products)) == RAIL_DEALS_TOTAL


def test_one_brand_cannot_take_every_slot():
    products = [FakeProduct(i, brand_id=1, retailer_id=i) for i in range(20)]
    assert len(select_rail_drops(products)) == RAIL_DEALS_PER_BRAND


def test_one_retailer_cannot_take_every_slot():
    """The case a brand-only cap missed: one outlet spanning many brands."""
    products = [FakeProduct(i, brand_id=i, retailer_id=99) for i in range(20)]
    assert len(select_rail_drops(products)) == RAIL_DEALS_PER_RETAILER


def test_keeps_best_first_ordering():
    products = [FakeProduct(i, brand_id=i, retailer_id=i) for i in range(20)]
    chosen = select_rail_drops(products)
    assert [p.id for p in chosen] == list(range(RAIL_DEALS_TOTAL))


def test_skipped_candidate_does_not_consume_a_slot():
    """A capped-out product is passed over, not counted against the total."""
    products = [
        FakeProduct(0, brand_id=1, retailer_id=1),
        FakeProduct(1, brand_id=1, retailer_id=2),
        FakeProduct(2, brand_id=1, retailer_id=3),  # brand 1 exhausted, skipped
        FakeProduct(3, brand_id=2, retailer_id=4),
    ]
    assert [p.id for p in select_rail_drops(products)] == [0, 1, 3]


def test_empty_input():
    assert select_rail_drops([]) == []
