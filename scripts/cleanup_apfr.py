"""One-time script to delete all products from APFR brand.

This script removes all products that were incorrectly matched to APFR
when it had aliases for A.P.C. fashion brand.
"""
import asyncio

from sqlalchemy import delete, func, select

from src.db.models import Brand, Product
from src.db.session import async_session


async def cleanup_apfr():
    """Delete all products from APFR brand."""
    async with async_session() as session:
        # Find APFR brand
        result = await session.execute(select(Brand).where(Brand.name == "APFR"))
        brand = result.scalar_one_or_none()

        if not brand:
            print("❌ APFR brand not found")
            return

        # Count products before deletion
        count_result = await session.execute(
            select(func.count(Product.id)).where(Product.brand_id == brand.id)
        )
        count = count_result.scalar()

        print(f"Found {count} products for APFR (brand_id={brand.id})")
        print(f"Current aliases: {brand.aliases}")

        if count == 0:
            print("✓ No products to delete")
            return

        # Delete all products (cascade deletes price_records)
        await session.execute(delete(Product).where(Product.brand_id == brand.id))
        await session.commit()

        print(f"✓ Deleted {count} products from APFR brand")


if __name__ == "__main__":
    asyncio.run(cleanup_apfr())
