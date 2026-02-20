#!/usr/bin/env python3
"""
Fix APFR Brand: Revert to incense/home goods only.

This script:
1. Reverts brand name from "A.P.C." back to "APFR" via API
2. Updates slug from "apc" back to "apfr" via SQL
3. Deletes incorrect A.P.C. fashion products from Gravity pope via SQL
4. Keeps APFR incense products from other retailers

Usage:
    python scripts/fix_apfr_keep_incense.py --password YOUR_DASHBOARD_PASSWORD --database-url YOUR_DATABASE_URL
"""

import argparse
import httpx
import psycopg2


def main():
    parser = argparse.ArgumentParser(description="Fix APFR brand - keep incense only")
    parser.add_argument("--password", required=True, help="Dashboard password")
    parser.add_argument("--database-url", required=True, help="PostgreSQL DATABASE_URL")
    parser.add_argument(
        "--prod-url",
        default="https://cheap-finder.onrender.com",
        help="Production URL",
    )
    args = parser.parse_args()

    # Step 1: Revert brand name via API
    print("Step 1: Reverting brand name to APFR via API...")
    client = httpx.Client(follow_redirects=True, timeout=30.0)

    login_resp = client.post(
        f"{args.prod_url}/login", data={"password": args.password}
    )
    if login_resp.status_code != 200:
        print(f"✗ Login failed: {login_resp.status_code}")
        return

    print("✓ Logged in")

    # Revert brand name and remove aliases
    update_resp = client.patch(
        f"{args.prod_url}/api/brands/1",
        json={"name": "APFR", "aliases": []},
    )

    if update_resp.status_code == 200:
        print("✓ Brand name reverted to APFR")
        print("✓ Aliases cleared")
    else:
        print(f"✗ Failed to update brand: {update_resp.status_code}")
        print(f"  Response: {update_resp.text}")
        return

    # Step 2 & 3: Update slug and delete products via SQL
    print("\nStep 2: Connecting to production database...")
    try:
        conn = psycopg2.connect(args.database_url)
        cursor = conn.cursor()
        print("✓ Connected to database")

        # Update slug
        print("\nStep 3: Updating slug to 'apfr'...")
        cursor.execute("UPDATE brands SET slug = 'apfr' WHERE id = 1;")
        print(f"✓ Slug updated ({cursor.rowcount} row)")

        # Get Gravity pope retailer ID
        print("\nStep 4: Finding Gravity pope retailer...")
        cursor.execute("SELECT id FROM retailers WHERE name = 'Gravity pope';")
        result = cursor.fetchone()

        if not result:
            print("✗ Gravity pope retailer not found")
            conn.rollback()
            return

        gravitypope_id = result[0]
        print(f"✓ Found Gravity pope (ID: {gravitypope_id})")

        # Count products before deletion
        print("\nStep 5: Counting A.P.C. products to delete...")
        cursor.execute(
            "SELECT COUNT(*) FROM products WHERE brand_id = 1 AND retailer_id = %s;",
            (gravitypope_id,),
        )
        count_before = cursor.fetchone()[0]
        print(f"  Found {count_before} A.P.C. products from Gravity pope")

        # Delete A.P.C. fashion products
        print("\nStep 6: Deleting A.P.C. fashion products...")
        cursor.execute(
            "DELETE FROM products WHERE brand_id = 1 AND retailer_id = %s;",
            (gravitypope_id,),
        )
        deleted_count = cursor.rowcount
        print(f"✓ Deleted {deleted_count} A.P.C. fashion products")

        # Count remaining products
        print("\nStep 7: Counting remaining APFR products...")
        cursor.execute("SELECT COUNT(*) FROM products WHERE brand_id = 1;")
        remaining = cursor.fetchone()[0]
        print(f"✓ {remaining} APFR incense products remaining")

        # Show remaining products by retailer
        print("\nRemaining products by retailer:")
        cursor.execute("""
            SELECT r.name, COUNT(p.id)
            FROM products p
            JOIN retailers r ON p.retailer_id = r.id
            WHERE p.brand_id = 1
            GROUP BY r.name
            ORDER BY COUNT(p.id) DESC;
        """)
        for retailer_name, product_count in cursor.fetchall():
            print(f"  - {retailer_name}: {product_count} products")

        # Commit changes
        conn.commit()
        print("\n✓ All changes committed to database")

    except Exception as e:
        print(f"\n✗ Database error: {e}")
        if conn:
            conn.rollback()
        return
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    print("\n" + "=" * 60)
    print("✓ APFR brand fix complete!")
    print("=" * 60)
    print("\nSummary:")
    print(f"  - Brand name: APFR")
    print(f"  - Slug: apfr")
    print(f"  - Aliases: [] (empty)")
    print(f"  - A.P.C. products deleted: {deleted_count}")
    print(f"  - APFR incense products remaining: {remaining}")
    print(f"\nVerify at: {args.prod_url}/brands/apfr")


if __name__ == "__main__":
    main()
