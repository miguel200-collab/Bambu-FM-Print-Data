"""
families.py — Group near-duplicate uploads into "design families".

Students rarely upload a design once. They re-slice with a changed setting, tweak
the model, rename the file and try again. A file hash alone cannot see that (the
five Benchy samples are the same model in five materials = five different
hashes), and treating those rows as independent would inflate any model's
accuracy: near-copies of one design would land on both the train and test side.

Three levels of identity (see the plan):

  file_sha256       the exact same file          -> features stored once
  gcode_md5         the same sliced toolpath     -> same G-code, different name
  design_family_id  "the same thing, tweaked"    -> this module

A family is **per student** (the same person iterating on their own design). A
new upload joins the closest existing family of that student, or starts a new one:

  * the student's family must have been active within ``FAMILY_WINDOW``;
  * names match   : the objects' names overlap (Jaccard >= ``NAME_JACCARD``),
                    AND the geometry isn't wildly different (guards against
                    generic names such as "Cube" / "Untitled" joining unrelated
                    designs);
  * OR geometry   : footprint area, both bounding-box sides and max Z are all
                    within ``GEOM_TOLERANCE`` (5%). Bounding-box sides are
                    compared sorted, so a part rotated 90 degrees on the plate
                    still matches.

The family fingerprint is that of its LATEST upload, so a design that drifts a few
percent per attempt is still followed.

Everything here is pure (no I/O), so it is cheap to test thoroughly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

FAMILY_WINDOW = timedelta(days=14)
GEOM_TOLERANCE = 0.05          # "about 5%" from the plan
NAME_GEOM_GUARD = 0.50         # same name but >50% different geometry => not a tweak
NAME_JACCARD = 0.5

# config_diff safety limits
_MAX_DIFF_KEYS = 200
_MAX_VALUE_LEN = 200


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fingerprint(features: dict[str, Any]) -> dict[str, Any]:
    """The few numbers/names that identify a design, from extract_features()."""
    curated = features.get("curated") or {}
    names = sorted({n for n in (_norm_name(x) for x in features.get("object_names") or []) if n})
    dims = [curated.get("bbox_x_mm"), curated.get("bbox_y_mm")]
    dims = sorted(d for d in dims if isinstance(d, (int, float)))
    return {
        "names": names,
        "area": curated.get("footprint_area_mm2"),
        "dims": dims if len(dims) == 2 else None,
        "max_z": curated.get("max_z_mm"),
    }


def _rel(a: Any, b: Any) -> Optional[float]:
    """Relative difference of two positive numbers; None if not comparable."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    hi = max(abs(a), abs(b))
    if hi == 0:
        return 0.0
    return abs(a - b) / hi


def geometry_distance(a: dict[str, Any], b: dict[str, Any]) -> Optional[float]:
    """
    Worst relative difference across the comparable geometry measurements
    (0 = identical). None when nothing is comparable.
    """
    diffs = [_rel(a.get("area"), b.get("area")), _rel(a.get("max_z"), b.get("max_z"))]
    if a.get("dims") and b.get("dims"):
        diffs += [_rel(x, y) for x, y in zip(a["dims"], b["dims"])]
    diffs = [d for d in diffs if d is not None]
    return max(diffs) if diffs else None


def names_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    na, nb = set(a.get("names") or []), set(b.get("names") or [])
    if not na or not nb:
        return False
    return len(na & nb) / len(na | nb) >= NAME_JACCARD


def score(new_fp: dict[str, Any], family_fp: dict[str, Any]) -> Optional[float]:
    """Lower is better; None = this family is not a candidate at all."""
    dist = geometry_distance(new_fp, family_fp)
    if names_match(new_fp, family_fp) and (dist is None or dist <= NAME_GEOM_GUARD):
        return (dist or 0.0) * 0.1            # name matches always beat geometry-only
    if dist is not None and dist <= GEOM_TOLERANCE:
        return 1.0 + dist
    return None


def choose_family(
    new_fp: dict[str, Any],
    new_time: datetime,
    candidates: Sequence[tuple[str, dict[str, Any], datetime]],
) -> Optional[str]:
    """
    Pick the family a new upload belongs to, or None to start a new one.

    ``candidates`` are ``(family_id, fingerprint, last_upload_at)`` for ONE
    student's existing families.
    """
    best: Optional[tuple[float, float, str]] = None
    for family_id, fp, last_at in candidates:
        if new_time - last_at > FAMILY_WINDOW:
            continue
        s = score(new_fp, fp)
        if s is None:
            continue
        key = (s, -last_at.timestamp(), family_id)   # tie -> most recently active
        if best is None or key < best:
            best = key
    return best[2] if best else None


def config_diff(
    previous: dict[str, str], current: dict[str, str]
) -> dict[str, list[Optional[str]]]:
    """Settings that changed between two attempts: ``{key: [old, new]}``."""
    out: dict[str, list[Optional[str]]] = {}
    for key in sorted(set(previous) | set(current)):
        old, new = previous.get(key), current.get(key)
        if old == new:
            continue
        out[key] = [
            None if old is None else old[:_MAX_VALUE_LEN],
            None if new is None else new[:_MAX_VALUE_LEN],
        ]
        if len(out) >= _MAX_DIFF_KEYS:
            break
    return out
