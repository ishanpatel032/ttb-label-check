"""Tests for the comparison rules. Run: python -m pytest, or python tests/test_matching.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.matching import (  # noqa: E402
    GOVERNMENT_WARNING, check_government_warning, compare_abv, compare_identity,
    compare_net_contents, parse_abv, parse_volume_ml, verify,
)


def test_brand_case_difference_is_a_match():
    # Dave's example from the discovery notes.
    r = compare_identity("brand_name", "Brand name", "Stone's Throw", "STONE'S THROW")
    assert r.status == "match"


def test_near_miss_brand_goes_to_a_person_not_to_a_rejection():
    r = compare_identity("class_type", "Class or type",
                         "Kentucky Straight Bourbon Whiskey",
                         "Kentucky Straight Bourbon Whisky")
    assert r.status == "review"


def test_different_brand_is_a_mismatch():
    r = compare_identity("brand_name", "Brand name", "Old Tom Distillery", "Blue Ridge Rye")
    assert r.status == "mismatch"


def test_warning_must_be_exact():
    assert check_government_warning(GOVERNMENT_WARNING).status == "match"


def test_title_case_prefix_is_rejected():
    # Jenny's example: correct wording, wrong capitalisation.
    bad = GOVERNMENT_WARNING.replace("GOVERNMENT WARNING:", "Government Warning:")
    r = check_government_warning(bad)
    assert r.status == "mismatch"
    assert "capital" in r.note


def test_reworded_warning_is_rejected():
    bad = GOVERNMENT_WARNING.replace("should not drink", "may wish to avoid")
    assert check_government_warning(bad).status == "mismatch"


def test_absent_warning_is_reported_as_missing():
    assert check_government_warning("").status == "missing"


def test_abv_parsing():
    assert parse_abv("45% Alc./Vol. (90 Proof)") == 45.0
    assert parse_abv("90 Proof") == 45.0
    assert parse_abv("45") == 45.0


def test_abv_difference_is_a_mismatch():
    assert compare_abv("45%", "43% Alc./Vol.").status == "mismatch"
    assert compare_abv("45%", "45% Alc./Vol. (90 Proof)").status == "match"


def test_proof_disagreeing_with_percentage_is_caught():
    assert compare_abv("45%", "45% Alc./Vol. (100 Proof)").status == "mismatch"


def test_volume_units_are_normalised():
    assert parse_volume_ml("1 L") == 1000.0
    assert compare_net_contents("750 mL", "750 ML").status == "match"
    assert compare_net_contents("750 mL", "375 mL").status == "mismatch"


def test_full_verify_flags_only_the_bad_field():
    application = {"brand_name": "Old Tom Distillery", "abv": "45%", "net_contents": "750 mL"}
    extracted = {"brand_name": "OLD TOM DISTILLERY", "abv": "43% Alc./Vol.",
                 "net_contents": "750 mL", "government_warning": GOVERNMENT_WARNING}
    out = verify(application, extracted)
    assert out["overall"] == "mismatch"
    assert out["counts"]["mismatch"] == 1


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"ok  {name}")
    print(f"\n{passed} passed")
