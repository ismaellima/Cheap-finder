from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from src.config import settings

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]


@dataclass
class ScrapedProduct:
    name: str
    url: str
    price: int  # cents CAD
    original_price: int | None = None
    on_sale: bool = False
    image_url: str = ""
    thumbnail_url: str = ""
    sku: str = ""
    gender: str = ""  # men, women, unisex, or empty
    brand: str = ""  # brand/vendor name from retailer (used for filtering)


@dataclass
class ScrapedPrice:
    price: int  # cents CAD
    original_price: int | None = None
    on_sale: bool = False
    currency: str = "CAD"
    available: bool = True


class RetailerBase(ABC):
    name: str = ""
    slug: str = ""
    base_url: str = ""
    requires_js: bool = False

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": random.choice(USER_AGENTS)},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _fetch(self, url: str) -> str:
        await asyncio.sleep(settings.REQUEST_DELAY_SECONDS)
        client = await self._get_client()
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text

    async def _fetch_soup(self, url: str) -> BeautifulSoup:
        html = await self._fetch(url)
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def parse_price(text: str) -> int | None:
        if not text:
            return None
        cleaned = text.replace("$", "").replace(",", "").replace("CAD", "").strip()
        try:
            return int(float(cleaned) * 100)
        except (ValueError, TypeError):
            return None

    @abstractmethod
    async def search_brand(self, brand_name: str) -> list[ScrapedProduct]:
        ...

    @abstractmethod
    async def get_price(self, product_url: str) -> ScrapedPrice | None:
        ...

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get(self.base_url)
            return resp.status_code == 200
        except Exception:
            logger.exception(f"Health check failed for {self.name}")
            return False

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} ({self.name})>"
