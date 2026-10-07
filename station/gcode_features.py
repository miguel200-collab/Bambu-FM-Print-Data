"""
gcode_features.py — Extract slicer settings and print statistics from a
sliced ``.gcode.3mf`` (a ZIP bundling G-code + plate metadata).

This is the cleaned-up successor of the lab's ad-hoc ``extract.py`` script. It
keeps what that script did (every ``; key = value`` line of the G-code
CONFIG_BLOCK) and fixes its gaps:

  * also parses the HEADER_BLOCK (``; key: value`` — estimated time, layers,
    filament length/weight, max Z), which the old script silently dropped;
  * reads ``Metadata/slice_info.config`` (XML: predicted seconds, grams,
    supports, printer model, object names) and ``Metadata/plate_N.json``
    (footprint area, bounding box, first-layer time);
  * handles multi-plate archives (picks the plate listed in slice_info);
  * reports ``is_sliced=False`` for a plain ``.3mf`` model instead of a row of
    "N/A", and raises a clean ``FeatureError`` for a file that is not a ZIP;
  * never swallows decode errors with a bare ``except``;
  * is a function (``extract_features(path)``), not a script with a hard-coded
    folder, and returns ``None`` — not ``"N/A"`` — for missing values.

The archive is attacker-controlled (any student can upload), so reads are
capped: oversized members are skipped, and XML containing a DOCTYPE/ENTITY is
refused rather than parsed.

Returned dict (JSON-serialisable)
---------------------------------
    extractor_version, is_sliced, error, gcode_md5, plate_count, plate_index,
    curated   {column: value}  - the ~50 promoted columns (see CURATED_COLUMNS)
    config    {key: raw str}   - every CONFIG_BLOCK setting, verbatim
    header    {key: raw str}   - HEADER_BLOCK values
    slice_info, plate_json     - parsed metadata for the chosen plate
    object_names               - names of the objects on the plate
    stats     {...}            - counts/distances measured from the G-code
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

EXTRACTOR_VERSION = 1

# G-code bigger than this is not scanned line-by-line (header/config still are).
MAX_GCODE_STATS_BYTES = 400 * 1024 * 1024
# Small metadata members (xml/json/md5) larger than this are ignored.
MAX_SMALL_MEMBER_BYTES = 8 * 1024 * 1024
# A real Bambu header+config block ends within the first ~600 lines.
_MAX_PREAMBLE_LINES = 3000

# Promoted columns of the file_features table: name -> SQLite type. Everything
# else stays available in raw_json. database.py builds the table from this.
CURATED_COLUMNS: dict[str, str] = {
    # provenance
    "slicer": "TEXT",
    "printer_model": "TEXT",
    "printer_model_id": "TEXT",
    "nozzle_diameter": "REAL",
    # size / time (from slice_info + header)
    "est_time_s": "REAL",
    "model_time_s": "REAL",
    "first_layer_time_s": "REAL",
    "layer_count": "INTEGER",
    "max_z_mm": "REAL",
    "filament_g": "REAL",
    "filament_mm": "REAL",
    "filament_type": "TEXT",
    "filament_count": "INTEGER",
    # geometry (from plate json)
    "object_count": "INTEGER",
    "footprint_area_mm2": "REAL",
    "bbox_x_mm": "REAL",
    "bbox_y_mm": "REAL",
    # slicer settings (from config block)
    "layer_height": "REAL",
    "initial_layer_height": "REAL",
    "infill_density": "REAL",
    "infill_pattern": "TEXT",
    "wall_loops": "INTEGER",
    "top_shell_layers": "INTEGER",
    "bottom_shell_layers": "INTEGER",
    "outer_wall_speed": "REAL",
    "inner_wall_speed": "REAL",
    "sparse_infill_speed": "REAL",
    "initial_layer_speed": "REAL",
    "travel_speed": "REAL",
    "nozzle_temp": "REAL",
    "nozzle_temp_initial": "REAL",
    "bed_type": "TEXT",
    "bed_temp": "REAL",
    "retraction_length": "REAL",
    "fan_max_speed": "REAL",
    "filament_max_volumetric_speed": "REAL",
    "support_used": "INTEGER",
    "support_type": "TEXT",
    "brim_type": "TEXT",
    "brim_width": "REAL",
    # measured from the G-code itself
    "retraction_count": "INTEGER",
    "extrusion_mm": "REAL",
    "travel_mm": "REAL",
    "travel_extrusion_ratio": "REAL",
    "bridge_feature_count": "INTEGER",
    "overhang_feature_count": "INTEGER",
    "support_feature_count": "INTEGER",
}

# curated column -> config key, parsed as the first number of a possibly
# comma-separated / percent value ("220,220", "15%").
_CONFIG_NUMBERS = {
    "layer_height": "layer_height",
    "initial_layer_height": "initial_layer_print_height",
    "infill_density": "sparse_infill_density",
    "outer_wall_speed": "outer_wall_speed",
    "inner_wall_speed": "inner_wall_speed",
    "sparse_infill_speed": "sparse_infill_speed",
    "initial_layer_speed": "initial_layer_speed",
    "travel_speed": "travel_speed",
    "nozzle_temp": "nozzle_temperature",
    "nozzle_temp_initial": "nozzle_temperature_initial_layer",
    "retraction_length": "retraction_length",
    "fan_max_speed": "fan_max_speed",
    "filament_max_volumetric_speed": "filament_max_volumetric_speed",
    "brim_width": "brim_width",
}
_CONFIG_INTS = {
    "wall_loops": "wall_loops",
    "top_shell_layers": "top_shell_layers",
    "bottom_shell_layers": "bottom_shell_layers",
}
_CONFIG_TEXT = {
    "infill_pattern": "sparse_infill_pattern",
    "support_type": "support_type",
    "brim_type": "brim_type",
    "bed_type": "curr_bed_type",
    "printer_model": "printer_model",
}

_PLATE_GCODE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")


class FeatureError(Exception):
    """The file cannot be read as a 3MF/ZIP archive at all."""


# ---------------------------------------------------------------------------
# Small parsers (pure)
# ---------------------------------------------------------------------------

def parse_duration(text: Optional[str]) -> Optional[float]:
    """'1h 6m 55s' / '58m 16s' / '2d 3h' -> seconds. None if nothing parses."""
    if not text:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text.lower())
    if not parts:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return float(sum(float(n) * mult[u] for n, u in parts))


def to_floats(value: Optional[str]) -> list[float]:
    """'220,220' -> [220.0, 220.0]; '15%' -> [15.0]; junk entries are skipped."""
    out: list[float] = []
    for piece in (value or "").split(","):
        piece = piece.strip().rstrip("%").strip()
        try:
            out.append(float(piece))
        except ValueError:
            continue
    return out


def first_number(value: Optional[str]) -> Optional[float]:
    nums = to_floats(value)
    return nums[0] if nums else None


def first_int(value: Optional[str]) -> Optional[int]:
    n = first_number(value)
    return int(n) if n is not None else None


def _snake(key: str) -> str:
    key = re.sub(r"\[[^\]]*\]", "", key)  # drop units like "[mm]"
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def parse_header_lines(lines: Iterable[str]) -> dict[str, str]:
    """
    HEADER_BLOCK lines look like ``; total layer number: 240`` or
    ``; model printing time: 58m 16s; total estimated time: 1h 6m 55s``.
    """
    out: dict[str, str] = {}
    for line in lines:
        body = line.lstrip(";").strip()
        if not body:
            continue
        # "; BambuStudio 02.03.00.70" has no colon: keep it as the slicer string.
        if ":" not in body:
            out.setdefault("slicer", body)
            continue
        for segment in body.split(";"):
            if ":" not in segment:
                continue
            key, value = segment.split(":", 1)
            key = _snake(key)
            if key:
                out[key] = value.strip()
    return out


# ---------------------------------------------------------------------------
# G-code scan (header + config + statistics) — single streaming pass
# ---------------------------------------------------------------------------

def _scan_gcode(stream, want_stats: bool) -> dict[str, Any]:
    header_lines: list[str] = []
    config: dict[str, str] = {}
    in_header = in_config = past_preamble = False

    layers = retractions = 0
    extrusion_mm = travel_mm = 0.0
    features: Counter[str] = Counter()
    x = y = 0.0

    for lineno, raw in enumerate(stream):
        if not past_preamble:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith(";"):
                if "HEADER_BLOCK_START" in line:
                    in_header = True
                elif "HEADER_BLOCK_END" in line:
                    in_header = False
                elif "CONFIG_BLOCK_START" in line:
                    in_config = True
                elif "CONFIG_BLOCK_END" in line:
                    in_config = False
                    past_preamble = True
                elif in_header:
                    header_lines.append(line)
                elif in_config and "=" in line:
                    key, value = line[1:].split("=", 1)
                    key = key.strip()
                    if key and " " not in key:
                        config[key] = value.strip()
                if (not past_preamble and lineno > _MAX_PREAMBLE_LINES
                        and not in_config and not in_header):
                    past_preamble = True  # no blocks found: stop looking for them
                if past_preamble and not want_stats:
                    break
                continue
            if line and not in_config and not in_header:
                # Real G-code before/without any header/config block (non-Bambu
                # file): the preamble is over. Fall through and count this line.
                past_preamble = True
                if not want_stats:
                    break
            else:
                continue

        # ---- statistics pass: raw bytes, no decoding of the common case ----
        s = raw.lstrip()
        if not s:
            continue
        if s[:1] == b";":
            if s.startswith(b"; CHANGE_LAYER"):
                layers += 1
            elif s.startswith(b"; FEATURE:"):
                features[s[10:].strip().decode("utf-8", errors="replace")] += 1
            continue
        if s[:1] != b"G" or s[1:2] not in b"0123" or (len(s) > 2 and s[2:3] not in b" \t\r\n"):
            continue  # not G0/G1/G2/G3 (G28, G92, M-codes, ...)

        nx, ny, e = x, y, None
        for token in s.split(b";", 1)[0].split()[1:]:
            tag = token[:1]
            if tag not in (b"X", b"Y", b"E"):
                continue
            try:
                value = float(token[1:])
            except ValueError:
                continue
            if tag == b"X":
                nx = value
            elif tag == b"Y":
                ny = value
            else:
                e = value
        moved = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y = nx, ny
        if e is not None and e < 0:
            retractions += 1          # pure retraction or retracting wipe
            travel_mm += moved
        elif e is not None and e > 0 and moved > 0:
            extrusion_mm += moved     # arcs (G2/G3) are approximated by their chord
        else:
            travel_mm += moved

    stats: dict[str, Any] = {"scanned": want_stats}
    if want_stats:
        total = extrusion_mm + travel_mm
        stats.update({
            "layer_count": layers,
            "retraction_count": retractions,
            "extrusion_mm": round(extrusion_mm, 2),
            "travel_mm": round(travel_mm, 2),
            "travel_extrusion_ratio": round(travel_mm / extrusion_mm, 4) if extrusion_mm > 0 else None,
            "path_total_mm": round(total, 2),
            "feature_counts": dict(features),
        })
    return {"header": parse_header_lines(header_lines), "config": config, "stats": stats}


# ---------------------------------------------------------------------------
# Archive members
# ---------------------------------------------------------------------------

def _read_small(z: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_SMALL_MEMBER_BYTES:
        return None
    return z.read(name)


def _parse_slice_info(data: Optional[bytes]) -> dict[str, Any]:
    """Parse Metadata/slice_info.config. Refuses DOCTYPE/ENTITY (XML bombs)."""
    empty: dict[str, Any] = {"plates": []}
    if not data:
        return empty
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        return empty
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return empty
    plates = []
    for plate in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata") if m.get("key")}
        plates.append({
            "index": first_int(meta.get("index")),
            "metadata": meta,
            "objects": [o.get("name") for o in plate.findall("object") if o.get("name")],
            "filaments": [dict(f.attrib) for f in plate.findall("filament")],
        })
    return {"plates": plates}


def _parse_json(data: Optional[bytes]) -> dict[str, Any]:
    if not data:
        return {}
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _config_from_project_settings(data: Optional[bytes]) -> dict[str, str]:
    """
    Fallback when the G-code has no CONFIG_BLOCK at the top (e.g. slicers that
    append it at the end): ``Metadata/project_settings.config`` is a JSON object
    with the same keys. It is NOT identical to the effective G-code config (some
    values are per-project, not per-print), so it is only used as a fallback.
    """
    out: dict[str, str] = {}
    for key, value in _parse_json(data).items():
        if isinstance(value, list):
            out[key] = ",".join(str(v) for v in value)
        elif isinstance(value, (str, int, float, bool)):
            out[key] = str(value)
    return out


def _bool(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    return 1 if str(text).strip().lower() in ("true", "1", "yes") else 0


def _bed_temp(config: dict[str, str]) -> Optional[float]:
    bed = (config.get("curr_bed_type") or "").lower()
    if "textured" in bed:
        key = "textured_plate_temp"
    elif "cool" in bed:
        key = "cool_plate_temp"
    elif "eng" in bed:
        key = "eng_plate_temp"
    elif "supertack" in bed:
        key = "supertack_plate_temp"
    else:
        key = "hot_plate_temp"
    return first_number(config.get(key))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _empty_result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "extractor_version": EXTRACTOR_VERSION,
        "is_sliced": False,
        "error": None,
        "gcode_md5": None,
        "plate_count": 0,
        "plate_index": None,
        "curated": {col: None for col in CURATED_COLUMNS},
        "config": {},
        "header": {},
        "slice_info": {},
        "plate_json": {},
        "object_names": [],
        "stats": {},
    }
    base.update(over)
    return base


def failed_result(error: str) -> dict[str, Any]:
    """A features record for a file that could not be read (stored so we don't retry forever)."""
    return _empty_result(error=error)


def extract_features(path: Path | str) -> dict[str, Any]:
    """
    Extract features from one ``.gcode.3mf``.

    Raises FeatureError if ``path`` is not a readable ZIP archive. A valid
    archive with no G-code (a plain ``.3mf`` model) returns ``is_sliced=False``.
    """
    path = Path(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise FeatureError(f"not a valid 3MF/ZIP archive: {exc}") from exc

    with z:
        plates = sorted(
            int(m.group(1)) for n in z.namelist() if (m := _PLATE_GCODE_RE.match(n))
        )
        if not plates:
            return _empty_result()

        slice_info = _parse_slice_info(_read_small(z, "Metadata/slice_info.config"))
        listed = [p["index"] for p in slice_info["plates"] if p["index"] in plates]
        plate_index = listed[0] if listed else plates[0]
        plate_info = next((p for p in slice_info["plates"] if p["index"] == plate_index), None)
        plate_json = _parse_json(_read_small(z, f"Metadata/plate_{plate_index}.json"))

        md5_raw = _read_small(z, f"Metadata/plate_{plate_index}.gcode.md5")
        gcode_md5 = md5_raw.decode("ascii", errors="ignore").strip().lower() if md5_raw else None
        gcode_md5 = gcode_md5 if gcode_md5 and re.fullmatch(r"[0-9a-f]{32}", gcode_md5) else None

        gcode_name = f"Metadata/plate_{plate_index}.gcode"
        want_stats = z.getinfo(gcode_name).file_size <= MAX_GCODE_STATS_BYTES
        try:
            with z.open(gcode_name) as fh:
                scanned = _scan_gcode(fh, want_stats)
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
            raise FeatureError(f"could not read G-code: {exc}") from exc
        config_source = "gcode"
        if not scanned["config"]:
            scanned["config"] = _config_from_project_settings(
                _read_small(z, "Metadata/project_settings.config")
            )
            config_source = "project_settings" if scanned["config"] else None

    header, config, stats = scanned["header"], scanned["config"], scanned["stats"]
    meta = (plate_info or {}).get("metadata", {})
    filaments = (plate_info or {}).get("filaments", [])
    object_names = (plate_info or {}).get("objects", [])

    curated: dict[str, Any] = {col: None for col in CURATED_COLUMNS}
    for col, key in _CONFIG_NUMBERS.items():
        curated[col] = first_number(config.get(key))
    for col, key in _CONFIG_INTS.items():
        curated[col] = first_int(config.get(key))
    for col, key in _CONFIG_TEXT.items():
        curated[col] = config.get(key) or None
    curated["bed_temp"] = _bed_temp(config)
    curated["brim_type"] = config.get("brim_type") or None

    curated["slicer"] = header.get("slicer") or (
        "BambuStudio " + meta["X-BBL-Client-Version"] if "X-BBL-Client-Version" in meta else None
    )
    curated["printer_model_id"] = meta.get("printer_model_id")
    nozzle = plate_json.get("nozzle_diameter")
    curated["nozzle_diameter"] = (
        round(float(nozzle), 3) if isinstance(nozzle, (int, float)) else None
    ) or first_number(meta.get("nozzle_diameters")) or first_number(config.get("nozzle_diameter"))

    curated["est_time_s"] = first_number(meta.get("prediction")) or parse_duration(
        header.get("total_estimated_time")
    )
    curated["model_time_s"] = parse_duration(header.get("model_printing_time"))
    curated["first_layer_time_s"] = (
        plate_json.get("first_layer_time") or first_number(meta.get("first_layer_time"))
    )
    curated["layer_count"] = first_int(header.get("total_layer_number")) or stats.get("layer_count")
    curated["max_z_mm"] = first_number(header.get("max_z_height"))
    weights = to_floats(header.get("total_filament_weight"))
    curated["filament_g"] = first_number(meta.get("weight")) or (sum(weights) if weights else None)
    lengths = to_floats(header.get("total_filament_length"))
    curated["filament_mm"] = sum(lengths) if lengths else None
    types = [f.get("type") for f in filaments if f.get("type")]
    curated["filament_type"] = ",".join(dict.fromkeys(types)) if types else (config.get("filament_type") or None)
    curated["filament_count"] = len(filaments) or (len(lengths) or None)

    boxes = plate_json.get("bbox_objects") or []
    areas = [b.get("area") for b in boxes if isinstance(b, dict) and isinstance(b.get("area"), (int, float))]
    curated["object_count"] = len(object_names) or (len(boxes) or None)
    curated["footprint_area_mm2"] = float(sum(areas)) if areas else None
    bbox = plate_json.get("bbox_all")
    if isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        curated["bbox_x_mm"] = round(float(bbox[2]) - float(bbox[0]), 3)
        curated["bbox_y_mm"] = round(float(bbox[3]) - float(bbox[1]), 3)

    support_used = _bool(meta.get("support_used"))
    curated["support_used"] = support_used if support_used is not None else _bool(config.get("enable_support"))

    fc = stats.get("feature_counts", {}) if stats.get("scanned") else {}
    if stats.get("scanned"):
        curated["retraction_count"] = stats["retraction_count"]
        curated["extrusion_mm"] = stats["extrusion_mm"]
        curated["travel_mm"] = stats["travel_mm"]
        curated["travel_extrusion_ratio"] = stats["travel_extrusion_ratio"]
        curated["bridge_feature_count"] = sum(c for n, c in fc.items() if "bridge" in n.lower())
        curated["overhang_feature_count"] = sum(c for n, c in fc.items() if "overhang" in n.lower())
        curated["support_feature_count"] = sum(c for n, c in fc.items() if "support" in n.lower())

    return _empty_result(
        is_sliced=True,
        gcode_md5=gcode_md5,
        plate_count=len(plates),
        plate_index=plate_index,
        curated=curated,
        config=config,
        header=header,
        slice_info={"plate": plate_info, "plate_count": len(slice_info["plates"])},
        plate_json={k: plate_json[k] for k in ("bbox_all", "bbox_objects", "bed_type", "is_seq_print",
                                               "first_layer_time", "nozzle_diameter") if k in plate_json},
        object_names=object_names,
        stats={**stats, "config_source": config_source},
    )
