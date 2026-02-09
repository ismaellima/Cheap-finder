from __future__ import annotations

from typing import Dict, Optional, Type

from src.retailers.base import RetailerBase
from src.retailers.generic import GenericScraper
from src.retailers.nrml import NRMLScraper
from src.retailers.livestock import LivestockScraper
from src.retailers.haven import HavenScraper
from src.retailers.simons import SimonsScraper
from src.retailers.ssense import SSENSEScraper
from src.retailers.altitude_sports import AltitudeSportsScraper
from src.retailers.sporting_life import SportingLifeScraper
from src.retailers.nordstrom import NordstromScraper
from src.retailers.hip_store import HipStoreScraper


def get_scraper_classes() -> Dict[str, Type[RetailerBase]]:
    """Return a mapping of retailer slug -> scraper class."""
    return {
        "nrml": NRMLScraper,
        "livestock": LivestockScraper,
        "haven": HavenScraper,
        "simons": SimonsScraper,
        "ssense": SSENSEScraper,
        "altitude_sports": AltitudeSportsScraper,
        "sporting_life": SportingLifeScraper,
        "nordstrom": NordstromScraper,
        "hip_store": HipStoreScraper,
        "generic": GenericScraper,
    }


def get_all_scrapers() -> Dict[str, RetailerBase]:
    """Return a mapping of retailer slug -> instantiated scraper."""
    return {slug: cls() for slug, cls in get_scraper_classes().items()}


def get_scraper(slug: str) -> RetailerBase:
    """Instantiate and return a scraper by retailer slug."""
    classes = get_scraper_classes()
    scraper_class = classes.get(slug)
    if scraper_class is None:
        raise ValueError(f"No scraper found for retailer slug: {slug}")
    return scraper_class()


def get_scraper_for_url(url: str) -> Optional[RetailerBase]:
    """Find the appropriate scraper for a given URL."""
    url_lower = url.lower()
    for slug, scraper_class in get_scraper_classes().items():
        if slug == "generic":
            continue
        instance = scraper_class()
        if instance.base_url and instance.base_url.lower() in url_lower:
            return instance
    # Fallback to generic
    return GenericScraper()
