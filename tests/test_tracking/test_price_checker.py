"""Tests for the rolling price-check batch.

The batch draws products least-recently-checked first, which only works if
every terminal outcome moves the product out of the way. If any outcome can
leave last_checked untouched, that product sits at the head of the queue and
is redrawn on every run, starving everything behind it — which is how the
original single-sweep job silently stopped covering most of the catalogue.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.db.models import Brand, Product, Retailer
from src.retailers.base import RetailerBase, ScrapedPrice
from src.tracking import price_checker
from src.tracking.price_checker import check_all_prices


class FakeScraper(RetailerBase):
    """Scraper stub that returns a canned price (or failure) and logs calls."""

    name = "Fake"
    slug = "fake"
    base_url = "https://fake.test"

    def __init__(self, price: ScrapedPrice | None) -> None:
        super().__init__()
        self._price = price
        self.requested: list[str] = []

    async def search_brand(self, brand_name: str) -> list:
        return []

    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        self.requested.append(product_url)
        return self._price


async def _seed(session, *, checked_at: list, scraper_type: str = "fake") -> list[Product]:
    """Create one retailer, one brand, and a product per last_checked value."""
    brand = Brand(name="Testbrand", slug="testbrand", aliases="[]", category="fashion")
    retailer = Retailer(
        name="Testshop",
        slug="testshop",
        base_url="https://fake.test",
        scraper_type=scraper_type,
    )
    session.add_all([brand, retailer])
    await session.flush()

    products = []
    for i, when in enumerate(checked_at):
        product = Product(
            name=f"Product {i}",
            brand_id=brand.id,
            retailer_id=retailer.id,
            url=f"https://fake.test/products/{i}",
            current_price=10_000,
            last_checked=when,
        )
        session.add(product)
        products.append(product)
    await session.commit()
    return products


@pytest.mark.asyncio
async def test_failed_check_still_advances_the_queue(db_session, monkeypatch):
    """A product the scraper can't read must not be redrawn forever.

    This is the starvation guard: without a stamp on the failure path, the
    same unreadable product heads every batch and nothing behind it is ever
    reached.
    """
    # Page still resolves — it's the scraper that failed, so nothing is deleted.
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _false())

    old = dt.datetime(2020, 1, 1)
    await _seed(db_session, checked_at=[old])
    scrapers = {"fake": FakeScraper(price=None)}

    stats = await check_all_prices(db_session, scrapers, batch_size=10)

    assert stats["failed"] == 1
    assert stats["removed"] == 0

    product = (await db_session.execute(_all_products())).scalars().one()
    assert product.last_checked > old, "failed check left the product at the queue head"


@pytest.mark.asyncio
async def test_batch_takes_stalest_first_and_respects_size(db_session, monkeypatch):
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _false())

    # Deliberately not in chronological order, plus a never-checked product.
    await _seed(
        db_session,
        checked_at=[
            dt.datetime(2024, 5, 1),  # 0 — middle
            dt.datetime(2020, 1, 1),  # 1 — oldest
            None,                     # 2 — never checked, must sort first
            dt.datetime(2026, 1, 1),  # 3 — newest
        ],
    )
    scraper = FakeScraper(price=ScrapedPrice(price=9_000))
    stats = await check_all_prices(db_session, {"fake": scraper}, batch_size=2)

    assert stats["batch"] == 2, "batch size was not respected"
    assert scraper.requested == [
        "https://fake.test/products/2",  # never checked
        "https://fake.test/products/1",  # oldest stamp
    ]


@pytest.mark.asyncio
async def test_products_without_a_scraper_are_not_drawn(db_session, monkeypatch):
    """Unscrapable retailers must not consume slots in every batch."""
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _false())

    await _seed(db_session, checked_at=[dt.datetime(2020, 1, 1)], scraper_type="ssense")
    scraper = FakeScraper(price=ScrapedPrice(price=9_000))

    stats = await check_all_prices(db_session, {"fake": scraper}, batch_size=10)

    assert stats["batch"] == 0
    assert scraper.requested == []


@pytest.mark.asyncio
async def test_confirmed_404_deletes_the_product(db_session, monkeypatch):
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _true())

    await _seed(db_session, checked_at=[dt.datetime(2020, 1, 1)])
    stats = await check_all_prices(db_session, {"fake": FakeScraper(price=None)}, batch_size=10)

    assert stats["removed"] == 1
    assert (await db_session.execute(_all_products())).scalars().all() == []


@pytest.mark.asyncio
async def test_successful_check_updates_price_and_stamp(db_session, monkeypatch):
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _false())

    old = dt.datetime(2020, 1, 1)
    await _seed(db_session, checked_at=[old])
    price = ScrapedPrice(price=7_500, original_price=10_000, on_sale=True)

    stats = await check_all_prices(db_session, {"fake": FakeScraper(price=price)}, batch_size=10)

    assert stats["checked"] == 1
    product = (await db_session.execute(_all_products())).scalars().one()
    assert product.current_price == 7_500
    assert product.on_sale is True
    assert product.last_checked > old


@pytest.mark.asyncio
async def test_out_of_stock_is_kept_but_stamped(db_session, monkeypatch):
    """Out of stock is a different state from removed — keep it, don't delete."""
    monkeypatch.setattr(price_checker, "_product_page_is_gone", lambda url: _false())

    old = dt.datetime(2020, 1, 1)
    await _seed(db_session, checked_at=[old])
    price = ScrapedPrice(price=7_500, available=False)

    stats = await check_all_prices(db_session, {"fake": FakeScraper(price=price)}, batch_size=10)

    assert stats["removed"] == 0
    product = (await db_session.execute(_all_products())).scalars().one()
    assert product.last_checked > old


def _all_products():
    from sqlalchemy import select

    return select(Product)


async def _false() -> bool:
    return False


async def _true() -> bool:
    return True
