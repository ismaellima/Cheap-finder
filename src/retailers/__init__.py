from src.retailers.base import RetailerBase
from src.retailers.generic import GenericScraper


def get_all_scrapers() -> dict[str, RetailerBase]:
    return {
        "generic": GenericScraper(),
    }
