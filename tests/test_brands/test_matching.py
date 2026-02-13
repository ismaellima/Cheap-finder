"""Unit tests for brand matching logic."""
from src.brands.discovery import _brand_matches, _normalize


def test_normalize_removes_punctuation():
    """Normalization should remove punctuation and spaces."""
    assert _normalize("Arc'teryx") == "arcteryx"
    assert _normalize("A.P.C.") == "apc"
    assert _normalize("Nike ACG") == "nikeacg"
    assert _normalize("On Running") == "onrunning"
    assert _normalize("Beams Plus") == "beamsplus"


def test_compound_brand_exact_match():
    """Compound brands should match exact normalized names."""
    assert _brand_matches("Nike ACG", "ACG", ["Nike ACG"]) is True
    assert _brand_matches("Nike ACG", "Nike ACG", []) is True
    assert _brand_matches("Beams Plus", "Beams Plus", []) is True
    assert _brand_matches("On Running", "On Cloud", ["On Running"]) is True
    assert _brand_matches("District Vision", "District Vision", []) is True


def test_compound_brand_no_substring():
    """Compound brands should NOT match via substring (THE KEY FIX)."""
    # Nike should NOT match Nike ACG
    assert _brand_matches("Nike", "ACG", ["Nike ACG"]) is False
    assert _brand_matches("Nike", "Nike ACG", []) is False

    # Beams should NOT match Beams Plus
    assert _brand_matches("Beams", "Beams Plus", []) is False

    # District should NOT match District Vision
    assert _brand_matches("District", "District Vision", []) is False

    # On should NOT match On Running (even though it's an alias)
    # Note: "On" is < 4 chars so wouldn't substring match anyway, but this tests the logic
    assert _brand_matches("On", "On Cloud", ["On Running"]) is False


def test_single_word_fuzzy_matching():
    """Single-word brands should still use fuzzy substring matching."""
    # Arc'teryx variations
    assert _brand_matches("Arcteryx", "Arc'teryx", []) is True
    assert _brand_matches("Arc-teryx", "Arc'teryx", []) is True

    # APFR / A.P.C. (all normalize to "apc" or "apfr")
    assert _brand_matches("APFR", "APFR", ["A.P.C."]) is True
    assert _brand_matches("APC", "APFR", ["A.P.C."]) is True

    # Satisfy variations
    assert _brand_matches("satisfy", "Satisfy", []) is True
    assert _brand_matches("SATISFY", "Satisfy", []) is True


def test_empty_vendor_always_matches():
    """Empty vendor should always match (can't verify)."""
    assert _brand_matches("", "Any Brand", []) is True
    assert _brand_matches("", "Nike ACG", ["Nike"]) is True
    assert _brand_matches("", "Arc'teryx", []) is True


def test_real_world_brands():
    """Test with actual brands from seed data."""
    # On Cloud with aliases (compound - has "On Running")
    assert _brand_matches("On", "On Cloud", ["On Running", "On"]) is True
    assert _brand_matches("On Running", "On Cloud", ["On Running", "On"]) is True

    # Satisfy Running (compound)
    assert _brand_matches("Satisfy", "Satisfy Running", ["Satisfy"]) is True
    assert _brand_matches("Satisfy Running", "Satisfy Running", ["Satisfy"]) is True

    # New Balance (compound - has "New Balance Made in USA")
    assert _brand_matches("New Balance", "New Balance", ["New Balance Made in USA"]) is True
    assert _brand_matches("New Balance Made in USA", "New Balance", ["New Balance Made in USA"]) is True

    # Beams Plus
    assert _brand_matches("Beams Plus", "Beams Plus", []) is True

    # District Vision
    assert _brand_matches("District Vision", "District Vision", []) is True


def test_minimum_length_still_applies():
    """Substring matching should still require >= 4 chars for single-word brands."""
    # "On" is < 4 chars, should only match exact
    assert _brand_matches("On", "On", []) is True

    # Brand name "ABCD" (4 chars) will substring match vendor "ABC" (3 chars)
    # because "abc" (3 chars) is IN "abcd" (4 chars), and "abcd" >= 4 chars
    # This is expected behavior - the brand name is long enough to allow fuzzy matching
    assert _brand_matches("ABC", "ABCD", []) is True  # "abc" in "abcd"

    # But brand name "AB" (2 chars) won't substring match
    assert _brand_matches("ABC", "AB", []) is False  # "ab" < 4 chars, no substring match


def test_case_insensitivity():
    """Matching should be case insensitive."""
    assert _brand_matches("nike acg", "ACG", ["Nike ACG"]) is True
    assert _brand_matches("NIKE ACG", "ACG", ["Nike ACG"]) is True
    assert _brand_matches("NiKe AcG", "ACG", ["Nike ACG"]) is True
    assert _brand_matches("arcteryx", "Arc'teryx", []) is True
    assert _brand_matches("ARCTERYX", "Arc'teryx", []) is True


def test_compound_with_punctuation():
    """Compound brands should normalize punctuation for exact match."""
    # "New Balance" with punctuation variations
    assert _brand_matches("New-Balance", "New Balance", []) is True
    assert _brand_matches("New.Balance", "New Balance", []) is True

    # But shouldn't substring match parts
    assert _brand_matches("New", "New Balance", []) is False
    assert _brand_matches("Balance", "New Balance", []) is False


def test_edge_case_multiple_spaces():
    """Multiple spaces in brand names should still be detected as compound."""
    assert _brand_matches("Brand  Name", "Brand  Name", []) is True
    # But NOT substring
    assert _brand_matches("Brand", "Brand  Name", []) is False


def test_aliases_array_empty():
    """Empty aliases list should work."""
    assert _brand_matches("Nike ACG", "Nike ACG", []) is True
    assert _brand_matches("Arc'teryx", "Arc'teryx", []) is True


def test_mixed_compound_and_single_aliases():
    """Brand with both single and compound aliases should use compound logic."""
    # If any alias has spaces, treat as compound (safer, more restrictive)
    assert _brand_matches("ACG", "ACG", ["ACG", "Nike ACG"]) is True  # exact match
    assert _brand_matches("Nike ACG", "ACG", ["ACG", "Nike ACG"]) is True  # exact match
    # But NOT substring
    assert _brand_matches("Nike", "ACG", ["ACG", "Nike ACG"]) is False
