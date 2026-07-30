"""Tests for the scraper status aggregation.

The interesting part is telling a working scrape from a broken one. Both stamp
Product.last_checked — that is deliberate, so the rolling batch can advance past
products it cannot read — so the stamp alone says nothing about success. Only a
successful fetch also writes a PriceRecord, and it does so in the same
transaction, landing at or just after the stamp.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.api.routes_dashboard import _scraper_stats
from src.db.models import Brand, PriceRecord, Product, Retailer


async def _seed(session, products):
    """products: list of (last_checked, newest_record_at)."""
    brand = Brand(name="B", slug="b", aliases="[]", category="fashion")
    retailer = Retailer(
        name="Shop", slug="shop", base_url="https://s.test", scraper_type="generic"
    )
    session.add_all([brand, retailer])
    await session.flush()

    for i, (checked, recorded) in enumerate(products):
        product = Product(
            name=f"P{i}",
            brand_id=brand.id,
            retailer_id=retailer.id,
            url=f"https://s.test/p/{i}",
            current_price=1000,
            last_checked=checked,
        )
        session.add(product)
        await session.flush()
        if recorded is not None:
            session.add(
                PriceRecord(product_id=product.id, price=1000, recorded_at=recorded)
            )
    await session.commit()


@pytest.mark.asyncio
async def test_counts_success_only_when_a_price_was_actually_recorded(db_session):
    now = dt.datetime.utcnow()
    recent = now - dt.timedelta(minutes=10)

    await _seed(
        db_session,
        [
            # checked and priced in the same transaction -> success
            (recent, recent + dt.timedelta(milliseconds=2)),
            # checked recently, but newest price is months old -> failure
            (recent, now - dt.timedelta(days=120)),
            # checked recently, never priced at all -> failure
            (recent, None),
        ],
    )

    stats = await _scraper_stats(db_session)

    assert stats["checked_total"] == 3
    assert stats["ok_total"] == 1
    assert stats["failed_total"] == 2
    assert stats["success_pct"] == pytest.approx(33.3, abs=0.1)


@pytest.mark.asyncio
async def test_products_checked_long_ago_are_outside_the_cycle(db_session):
    now = dt.datetime.utcnow()
    stale = now - dt.timedelta(hours=48)

    await _seed(db_session, [(stale, stale), (None, None)])

    stats = await _scraper_stats(db_session)

    assert stats["checked_total"] == 0, "a 48h-old check is not part of this cycle"
    assert stats["tracked_total"] == 2
    assert stats["remaining"] == 2
    assert stats["coverage_pct"] == 0.0
    assert stats["success_pct"] is None


@pytest.mark.asyncio
async def test_running_flag_reflects_very_recent_activity(db_session):
    now = dt.datetime.utcnow()
    await _seed(db_session, [(now - dt.timedelta(minutes=1), now)])
    assert (await _scraper_stats(db_session))["running"] is True


@pytest.mark.asyncio
async def test_idle_when_last_activity_is_old(db_session):
    now = dt.datetime.utcnow()
    await _seed(db_session, [(now - dt.timedelta(minutes=45), now)])
    assert (await _scraper_stats(db_session))["running"] is False


@pytest.mark.asyncio
async def test_empty_database_does_not_divide_by_zero(db_session):
    stats = await _scraper_stats(db_session)
    assert stats["coverage_pct"] == 0.0
    assert stats["success_pct"] is None
    assert stats["retailers"] == []
