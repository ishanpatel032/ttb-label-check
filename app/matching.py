"""
Comparison logic between what the application says and what the label shows.

Two different standards apply here, on purpose:

  * Identity fields (brand, class/type, producer, origin) are compared
    leniently. "STONE'S THROW" and "Stone's Throw" are the same brand.
    Case, punctuation and spacing differences are normalised away, and a
    close-but-not-identical result is flagged for a human rather than
    failed outright.

  * The government health warning is compared strictly. 27 CFR 16.21
    prescribes exact wording, and the "GOVERNMENT WARNING:" prefix must
    appear in capitals. A near-match here is a finding, not a pass.

No model is involved in this file. Extraction is the fuzzy part; once we
have text, the rules are deterministic and testable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher

# 27 CFR 16.21. Mandatory on every alcohol beverage container.
GOVERNMENT_WARNING = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should "
    "not drink alcoholic beverages during pregnancy because of the risk of "
    "birth defects. (2) Consumption of alcoholic beverages impairs your "
    "ability to drive a car or operate machinery, and may cause health "
    "problems."
)

MATCH = "match"
REVIEW = "review"
MISMATCH = "mismatch"
MISSING = "missing"

# Above this, the difference is cosmetic. Below FAIL_AT, it is a real
# difference. In between, a person looks at it.
REVIEW_AT = 0.90
FAIL_AT = 0.75


@dataclass
class FieldResult:
    field: str
    label: str
    expected: str
    found: str
    status: str
    note: str = ""
    similarity: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# normalisation helpers
# --------------------------------------------------------------------------

_SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-",
}


def _clean(text: str) -> str:
    """Unicode-normalise, straighten quotes, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _SMART_QUOTES.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip()


def _loose(text: str) -> str:
    """Aggressive form used for identity comparisons: casefolded, punctuation
    dropped, common company suffixes removed."""
    text = _clean(text).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(
        r"\b(inc|llc|ltd|co|corp|company|the)\b", " ", text
    )
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _loose(a), _loose(b)).ratio()


# --------------------------------------------------------------------------
# field comparisons
# --------------------------------------------------------------------------

def compare_identity(field: str, label: str, expected: str, found: str) -> FieldResult:
    expected, found = _clean(expected), _clean(found)

    if not expected:
        return FieldResult(field, label, expected, found, MATCH,
                           "Not supplied in the application, so not checked.")
    if not found:
        return FieldResult(field, label, expected, found, MISSING,
                           "Not found on the label.")

    score = similarity(expected, found)

    if score >= 0.999:
        note = ""
        if _clean(expected) != found:
            note = "Same text, different capitalisation or punctuation."
        return FieldResult(field, label, expected, found, MATCH, note, score)
    if score >= REVIEW_AT:
        return FieldResult(field, label, expected, found, REVIEW,
                           "Very close but not identical. Confirm before approving.", score)
    if score >= FAIL_AT:
        return FieldResult(field, label, expected, found, REVIEW,
                           "Partial match only.", score)
    return FieldResult(field, label, expected, found, MISMATCH,
                       "Label does not match the application.", score)


_ABV_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%")
_PROOF_RE = re.compile(r"(\d{1,3}(?:\.\d)?)\s*proof", re.I)


def parse_abv(text: str) -> float | None:
    """Pull an alcohol-by-volume percentage out of free text.

    Handles '45% Alc./Vol.', 'ALC 45% BY VOL', and falls back to proof
    (90 Proof -> 45.0) when no percentage is printed."""
    if not text:
        return None
    text = _clean(text)
    m = _ABV_RE.search(text)
    if m:
        return float(m.group(1))
    m = _PROOF_RE.search(text)
    if m:
        return float(m.group(1)) / 2
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d{1,2})?)\s*", text)
    return float(m.group(1)) if m else None


def compare_abv(expected: str, found: str, tolerance: float = 0.0) -> FieldResult:
    f = "abv"
    label = "Alcohol content"
    exp_val, got_val = parse_abv(expected), parse_abv(found)

    if not _clean(expected):
        return FieldResult(f, label, expected, found, MATCH,
                           "Not supplied in the application, so not checked.")
    if not _clean(found):
        return FieldResult(f, label, expected, found, MISSING,
                           "No alcohol content found on the label.")
    if exp_val is None or got_val is None:
        return FieldResult(f, label, expected, found, REVIEW,
                           "Could not read a percentage from one of these.")

    # A label that contradicts itself is a finding regardless of the
    # application, so this is checked before anything can pass.
    pm = _PROOF_RE.search(_clean(found))
    if pm and _ABV_RE.search(_clean(found)):
        if abs(float(pm.group(1)) / 2 - got_val) > 0.05:
            return FieldResult(
                f, label, expected, found, MISMATCH,
                f"The label contradicts itself: {got_val:g}% is "
                f"{got_val * 2:g} proof, not {float(pm.group(1)):g}.")

    if abs(exp_val - got_val) <= tolerance:
        note = ""
        if _clean(expected) != _clean(found):
            note = f"Both read as {got_val:g}% alcohol by volume."
        return FieldResult(f, label, expected, found, MATCH, note)

    return FieldResult(f, label, expected, found, MISMATCH,
                       f"Application says {exp_val:g}%, label shows {got_val:g}%.")


_UNITS_TO_ML = {
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0, "millilitre": 1.0,
    "cl": 10.0, "l": 1000.0, "liter": 1000.0, "liters": 1000.0,
    "litre": 1000.0, "litres": 1000.0,
    "floz": 29.5735, "flozs": 29.5735, "ounce": 29.5735, "ounces": 29.5735,
}
_VOL_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|milliliters?|millilitres?|cl|l|liters?|litres?|"
    r"fl\.?\s*oz\.?|ounces?)",
    re.I,
)


def parse_volume_ml(text: str) -> float | None:
    if not text:
        return None
    m = _VOL_RE.search(_clean(text))
    if not m:
        return None
    qty = float(m.group(1).replace(",", "."))
    unit = re.sub(r"[.\s]", "", m.group(2)).lower()
    factor = _UNITS_TO_ML.get(unit)
    return qty * factor if factor else None


def compare_net_contents(expected: str, found: str) -> FieldResult:
    f = "net_contents"
    label = "Net contents"
    if not _clean(expected):
        return FieldResult(f, label, expected, found, MATCH,
                           "Not supplied in the application, so not checked.")
    if not _clean(found):
        return FieldResult(f, label, expected, found, MISSING,
                           "No net contents found on the label.")

    exp_ml, got_ml = parse_volume_ml(expected), parse_volume_ml(found)
    if exp_ml is None or got_ml is None:
        return compare_identity(f, label, expected, found)

    # 1% covers rounding between metric and imperial statements.
    if abs(exp_ml - got_ml) <= max(exp_ml, got_ml) * 0.01:
        note = ""
        if _loose(expected) != _loose(found):
            note = f"Both equal about {got_ml:g} mL."
        return FieldResult(f, label, expected, found, MATCH, note)
    return FieldResult(f, label, expected, found, MISMATCH,
                       f"Application is {exp_ml:g} mL, label is {got_ml:g} mL.")


def check_government_warning(found_raw: str) -> FieldResult:
    """Strict check. Wording must be exact; the prefix must be in capitals."""
    f = "government_warning"
    label = "Government warning"
    found = _clean(found_raw)

    if not found:
        return FieldResult(f, label, GOVERNMENT_WARNING, "", MISSING,
                           "No government warning found on the label. "
                           "This statement is mandatory.")

    problems: list[str] = []

    if "GOVERNMENT WARNING:" not in found:
        if re.search(r"government\s+warning", found, re.I):
            problems.append(
                'The "GOVERNMENT WARNING:" prefix is not in capital letters.'
            )
        else:
            problems.append('The "GOVERNMENT WARNING:" prefix is missing.')

    # Wording is compared without case so that a capitalisation fault is
    # reported once, as a capitalisation fault, rather than twice.
    if _clean(found).casefold() != GOVERNMENT_WARNING.casefold():
        score = SequenceMatcher(
            None, found.casefold(), GOVERNMENT_WARNING.casefold()
        ).ratio()
        if score < 0.995:
            problems.append(
                "Wording differs from the text required by 27 CFR 16.21."
            )
    else:
        score = 1.0

    if not problems:
        return FieldResult(f, label, GOVERNMENT_WARNING, found, MATCH,
                           "Exact match, prefix correctly capitalised.", 1.0)
    return FieldResult(f, label, GOVERNMENT_WARNING, found, MISMATCH,
                       " ".join(problems), score)


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

IDENTITY_FIELDS = [
    ("brand_name", "Brand name"),
    ("class_type", "Class or type"),
    ("producer", "Bottler or producer"),
    ("country_of_origin", "Country of origin"),
]


def verify(application: dict, extracted: dict) -> dict:
    """Compare an application record against fields read off the label."""
    results: list[FieldResult] = []

    for key, label in IDENTITY_FIELDS:
        expected = (application.get(key) or "").strip()
        if key == "country_of_origin" and not expected:
            continue  # only required for imports
        results.append(
            compare_identity(key, label, expected, (extracted.get(key) or "").strip())
        )

    results.append(compare_abv(application.get("abv", ""), extracted.get("abv", "")))
    results.append(
        compare_net_contents(
            application.get("net_contents", ""), extracted.get("net_contents", "")
        )
    )
    results.append(check_government_warning(extracted.get("government_warning", "")))

    counts = {MATCH: 0, REVIEW: 0, MISMATCH: 0, MISSING: 0}
    for r in results:
        counts[r.status] += 1

    if counts[MISMATCH] or counts[MISSING]:
        overall = MISMATCH
        headline = "Findings to resolve"
    elif counts[REVIEW]:
        overall = REVIEW
        headline = "Needs a look"
    else:
        overall = MATCH
        headline = "Everything matches"

    return {
        "overall": overall,
        "headline": headline,
        "counts": counts,
        "fields": [r.to_dict() for r in results],
    }
