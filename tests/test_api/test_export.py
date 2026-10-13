"""Tests for the paginated full export and the sync script that reads it.

The export used to build every product and price record into one dict, which
ran prod's 512 MB instance out of memory. These check that the pages cover every
row exactly once, that brand aliases still come through, and that the streamed
/export-full and the page-by-page fetch in scripts/sync_from_prod.py produce the
same data.
"""
from __future__ import annotations

import datetime as dt
import functools
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api import routes_brands
from src.db.models import Base, Brand, BrandRetailer, PriceRecord, Product, Retailer
from src.db.session import get_session

N_PRODUCTS = 7
RECORDS_PER_PRODUCT = 3


@pytest_asyncio.fixture
async def session_factory(monkeypatch):
    # StaticPool so every session sees the same in-memory database
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(routes_brands, "async_session", factory)

    async with factory() as session:
        brand = Brand(
            name="A.P.C.", slug="apc", aliases=json.dumps(["APFR", "A.P.C"]), category="fashion"
        )
        retailer = Retailer(
            name="Shop", slug="shop", base_url="https://s.test", scraper_type="generic"
        )
        session.add_all([brand, retailer])
        await session.flush()
        session.add(BrandRetailer(brand_id=brand.id, retailer_id=retailer.id, brand_url="/apc"))
        t0 = dt.datetime(2026, 1, 1)
        for i in range(N_PRODUCTS):
            product = Product(
                name=f"P{i}",
                brand_id=brand.id,
                retailer_id=retailer.id,
                url=f"https://s.test/p/{i}",
                current_price=1000 + i,
                last_checked=t0,
            )
            session.add(product)
            await session.flush()
            for j in range(RECORDS_PER_PRODUCT):
                session.add(
                    PriceRecord(
                        product_id=product.id,
                        price=1000 + i + j,
                        recorded_at=t0 + dt.timedelta(days=j),
                    )
                )
        await session.commit()

    yield factory
    await engine.dispose()


@pytest.fixture
def app(session_factory) -> FastAPI:
    app = FastAPI()
    app.include_router(routes_brands.export_router)

    async def _get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    return app


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _all_pages(client, path: str, limit: int) -> list[dict]:
    items, after_id, requests = [], 0, 0
    while after_id is not None:
        resp = await client.get(path, params={"after_id": after_id, "limit": limit})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) <= limit
        items.extend(body["items"])
        after_id = body["next_after_id"]
        requests += 1
        assert requests < 100, "pagination did not terminate"
    return items


async def test_meta_includes_aliases_mappings_and_counts(client):
    body = (await client.get("/api/export-full/meta")).json()

    assert body["brands"][0]["slug"] == "apc"
    assert body["brands"][0]["aliases"] == ["APFR", "A.P.C"]
    assert body["retailers"][0]["slug"] == "shop"
    assert body["brand_retailers"] == [
        {"brand_slug": "apc", "retailer_slug": "shop", "brand_url": "/apc", "verified": False}
    ]
    assert body["counts"] == {
        "products": N_PRODUCTS,
        "price_records": N_PRODUCTS * RECORDS_PER_PRODUCT,
    }


@pytest.mark.parametrize("limit", [1, 3, N_PRODUCTS, 1000])
async def test_product_pages_cover_every_row_once(client, limit):
    items = await _all_pages(client, "/api/export-full/products", limit)

    urls = [p["url"] for p in items]
    assert sorted(urls) == sorted(f"https://s.test/p/{i}" for i in range(N_PRODUCTS))
    assert len(set(urls)) == len(urls)
    assert items[0]["brand_slug"] == "apc"
    assert items[0]["retailer_slug"] == "shop"
    assert items[0]["last_checked"] == "2026-01-01T00:00:00"


@pytest.mark.parametrize("limit", [1, 4, N_PRODUCTS * RECORDS_PER_PRODUCT])
async def test_price_record_pages_cover_every_row_once(client, limit):
    items = await _all_pages(client, "/api/export-full/price-records", limit)

    assert len(items) == N_PRODUCTS * RECORDS_PER_PRODUCT
    keys = {(r["product_url"], r["recorded_at"]) for r in items}
    assert len(keys) == len(items)
    assert all(r["currency"] == "CAD" for r in items)


async def test_page_limit_is_capped(client):
    resp = await client.get(
        "/api/export-full/products",
        params={"limit": routes_brands.EXPORT_PAGE_MAX + 1},
    )
    assert resp.status_code == 422


async def test_streamed_export_full_is_valid_json_with_every_row(client):
    resp = await client.get("/api/export-full")

    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert set(body) == {"brands", "retailers", "brand_retailers", "products", "price_records"}
    assert body["brands"][0]["aliases"] == ["APFR", "A.P.C"]
    assert len(body["products"]) == N_PRODUCTS
    assert len(body["price_records"]) == N_PRODUCTS * RECORDS_PER_PRODUCT


async def test_streamed_export_full_handles_empty_tables(client, session_factory):
    async with session_factory() as session:
        await session.execute(PriceRecord.__table__.delete())
        await session.execute(Product.__table__.delete())
        await session.commit()

    body = json.loads((await client.get("/api/export-full")).content)
    assert body["products"] == []
    assert body["price_records"] == []


def _load_sync_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sync_from_prod.py"
    spec = importlib.util.spec_from_file_location("sync_from_prod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_sync_script_fetch_matches_streamed_export(app, client, monkeypatch):
    sync = _load_sync_script()
    monkeypatch.setattr(sync, "PAGE_SIZE", 2)
    monkeypatch.setattr(
        sync.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.ASGITransport(app=app)),
    )

    fetched = await sync.fetch_export("http://test")
    streamed = json.loads((await client.get("/api/export-full")).content)

    assert fetched == streamed
