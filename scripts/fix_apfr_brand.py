#!/usr/bin/env python3
"""
Fix APFR brand: Update slug and clean up incorrect products.

This script:
1. Updates the brand slug from 'apfr' to 'apc' in production
2. Deletes incorrect APFR incense products from specific retailers

Usage:
    python scripts/fix_apfr_brand.py --password YOUR_DASHBOARD_PASSWORD
"""

import argparse
import httpx


def main():
    parser = argparse.ArgumentParser(description="Fix APFR brand in production")
    parser.add_argument("--password", required=True, help="Dashboard password")
    parser.add_argument(
        "--prod-url",
        default="https://cheap-finder.onrender.com",
        help="Production URL",
    )
    args = parser.parse_args()

    client = httpx.Client(follow_redirects=True, timeout=30.0)

    # Login
    print(f"Logging in to {args.prod_url}...")
    login_resp = client.post(
        f"{args.prod_url}/login", data={"password": args.password}
    )
    if login_resp.status_code != 200:
        print(f"Login failed: {login_resp.status_code}")
        return

    print("✓ Logged in")

    # Get all brands
    print("\nFetching brands...")
    brands_resp = client.get(f"{args.prod_url}/api/brands")
    brands = brands_resp.json()

    # Find APFR brand
    apfr_brand = None
    for brand in brands:
        if brand.get("slug") == "apfr" or brand.get("name") == "A.P.C.":
            apfr_brand = brand
            break

    if not apfr_brand:
        print("✗ APFR/A.P.C. brand not found")
        return

    print(f"✓ Found brand ID {apfr_brand['id']}: {apfr_brand['name']}")
    print(f"  Current slug: {apfr_brand['slug']}")
    print(f"  Current aliases: {apfr_brand.get('aliases', [])}")

    # Note: Slug update requires direct DB access (not in API)
    if apfr_brand["slug"] != "apc":
        print(
            "\n⚠️  Slug update required (not available via API):"
        )
        print("  Run this SQL on the production database:")
        print(f"  UPDATE brands SET slug = 'apc' WHERE id = {apfr_brand['id']};")

    # Get all retailers to find the ones with incorrect products
    print("\nFetching retailers...")
    retailers_resp = client.get(f"{args.prod_url}/api/retailers")
    retailers = retailers_resp.json()

    # Find retailer IDs for Livestock, Blue Button Shop, Annms
    incorrect_retailers = {}
    for retailer in retailers:
        if retailer["name"] in ["Livestock", "Blue Button Shop", "Annms"]:
            incorrect_retailers[retailer["name"]] = retailer["id"]

    if incorrect_retailers:
        print(f"✓ Found {len(incorrect_retailers)} retailers with incorrect products:")
        for name, rid in incorrect_retailers.items():
            print(f"  - {name} (ID: {rid})")

        print(
            "\n⚠️  Product deletion required (not available via API):"
        )
        print("  Run this SQL on the production database:")
        retailer_ids = ", ".join(str(rid) for rid in incorrect_retailers.values())
        print(f"""
  DELETE FROM products
  WHERE brand_id = {apfr_brand['id']}
    AND retailer_id IN ({retailer_ids});
""")

        print("\n  OR delete manually via the dashboard:")
        print(f"  1. Go to {args.prod_url}/brands/{apfr_brand['slug']}")
        print("  2. Find products from Livestock, Blue Button Shop, Annms")
        print("  3. Delete products with names like:")
        print("     - Incense Sticks")
        print("     - Fragrance Candle")
        print("     - Tin Candle")
        print("     - Brass Incense Stand")
    else:
        print("✓ No incorrect retailers found (already cleaned?)")

    print("\n✓ Done!")
    print(
        "\nAfter updating slug and cleaning products, re-run discovery on Gravity pope"
    )
    print(f"to verify only A.P.C. fashion items are matched.")


if __name__ == "__main__":
    main()
