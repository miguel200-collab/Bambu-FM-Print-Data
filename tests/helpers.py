"""Builders for synthetic .gcode.3mf archives with known, hand-computable values."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Optional

# Config values every synthetic file starts with. Tests override single keys.
BASE_CONFIG = {
    "layer_height": "0.2",
    "initial_layer_print_height": "0.2",
    "sparse_infill_density": "15%",
    "sparse_infill_pattern": "grid",
    "wall_loops": "2",
    "top_shell_layers": "5",
    "bottom_shell_layers": "3",
    "outer_wall_speed": "200",
    "inner_wall_speed": "300",
    "sparse_infill_speed": "270",
    "initial_layer_speed": "50",
    "travel_speed": "500",
    "nozzle_temperature": "220,220",
    "nozzle_temperature_initial_layer": "220",
    "curr_bed_type": "Textured PEI Plate",
    "textured_plate_temp": "55",
    "hot_plate_temp": "60",
    "cool_plate_temp": "35",
    "enable_support": "0",
    "support_type": "tree(auto)",
    "brim_type": "auto_brim",
    "brim_width": "5",
    "filament_type": "PLA",
    "printer_model": "Bambu Lab X1 Carbon",
    "nozzle_diameter": "0.4",
    "retraction_length": "0.8",
    "fan_max_speed": "100",
    "filament_max_volumetric_speed": "12",
}

# Known toolpath body (positions start at 0,0; absolute XY, relative E):
#   2 layers; 1 pure retraction; travel 10 (to X10) + 10 (back to 0,0) = 20 mm;
#   extrusion 10 + 10 = 20 mm; lines that look like moves but are not (G28, G29 and
#   G30 probes WITH coordinates, G92, G10, M104) must be ignored.
GCODE_BODY = """\
G28
G29 X100 Y100
G30 X50 Y50 E5
G92 E0
M104 S200
; CHANGE_LAYER
; FEATURE: Outer wall
G1 E-.8 F1800
G1 X10 Y0 F6000
G1 X10 Y10 E.5
; FEATURE: Bridge
G1 X0 Y10 E.5
; CHANGE_LAYER
; FEATURE: Overhang wall
G10
G0 X0 Y0
"""


def gcode_text(
    *,
    config: dict[str, str],
    est_total: str = "1h 6m 55s",
    model_time: str = "58m 16s",
    layers: int = 2,
    weight: str = "11.84",
    length: str = "4036.46",
    max_z: str = "48.00",
    blocks: bool = True,
) -> str:
    if not blocks:
        return GCODE_BODY
    header = (
        "; HEADER_BLOCK_START\n"
        "; BambuStudio 02.03.00.70\n"
        f"; model printing time: {model_time}; total estimated time: {est_total}\n"
        f"; total layer number: {layers}\n"
        f"; total filament length [mm] : {length}\n"
        "; total filament volume [cm^3] : 9708.83\n"
        f"; total filament weight [g] : {weight}\n"
        "; filament_density: 1.22\n"
        f"; max_z_height: {max_z}\n"
        "; HEADER_BLOCK_END\n\n"
    )
    cfg = "; CONFIG_BLOCK_START\n" + "".join(f"; {k} = {v}\n" for k, v in config.items()) + "; CONFIG_BLOCK_END\n\n"
    return header + cfg + "; EXECUTABLE_BLOCK_START\n" + GCODE_BODY + "; EXECUTABLE_BLOCK_END\n"


def slice_info_xml(plates: list[dict]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", "  <header>",
           '    <header_item key="X-BBL-Client-Version" value="02.03.00.70"/>', "  </header>"]
    for p in plates:
        out.append("  <plate>")
        out.append(f'    <metadata key="index" value="{p["index"]}"/>')
        out.append(f'    <metadata key="prediction" value="{p.get("prediction", 3371)}"/>')
        out.append(f'    <metadata key="weight" value="{p.get("weight", 9.61)}"/>')
        out.append(f'    <metadata key="support_used" value="{p.get("support_used", "false")}"/>')
        out.append('    <metadata key="printer_model_id" value="BL-P001"/>')
        for name in p.get("objects", ["Part"]):
            out.append(f'    <object identify_id="1" name="{name}" skipped="false" />')
        out.append(f'    <filament id="1" type="{p.get("filament", "PLA")}" used_g="9.61" />')
        out.append("  </plate>")
    out.append("</config>")
    return "\n".join(out)


def plate_json(*, area: float, bbox: tuple[float, float, float, float], name: str = "Part") -> str:
    return json.dumps({
        "bbox_all": list(bbox),
        "bbox_objects": [{"area": area, "bbox": list(bbox), "id": 1, "layer_height": 0.2, "name": name}],
        "first_layer_time": 58.5,
        "nozzle_diameter": 0.4000000059604645,
        "bed_type": "textured_plate",
        "is_seq_print": False,
    })


def build_3mf(
    path: Path,
    *,
    names: Optional[list[str]] = None,
    area: float = 454.4,
    dims: tuple[float, float] = (39.0, 16.2),
    max_z: str = "48.00",
    config_overrides: Optional[dict[str, str]] = None,
    plates: int = 1,
    listed_first: Optional[int] = None,
    sliced: bool = True,
    blocks: bool = True,
    est_total: str = "1h 6m 55s",
    slice_info: Optional[str] = None,
    md5: Optional[str] = None,
    extra_members: Optional[dict[str, bytes]] = None,
) -> Path:
    """Write a synthetic .gcode.3mf to ``path`` and return it."""
    names = names or ["Part"]
    config = {**BASE_CONFIG, **(config_overrides or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        if sliced:
            order = list(range(1, plates + 1))
            if listed_first is not None:
                order.remove(listed_first)
                order.insert(0, listed_first)
            for n in range(1, plates + 1):
                gcode = gcode_text(config=config, max_z=max_z, est_total=est_total, blocks=blocks)
                z.writestr(f"Metadata/plate_{n}.gcode", gcode)
                z.writestr(
                    f"Metadata/plate_{n}.gcode.md5",
                    (md5 or hashlib.md5(gcode.encode()).hexdigest()).upper(),
                )
                x0, y0 = 100.0 + 10 * n, 100.0
                z.writestr(f"Metadata/plate_{n}.json",
                           plate_json(area=area, bbox=(x0, y0, x0 + dims[0], y0 + dims[1]), name=names[0]))
            z.writestr(
                "Metadata/slice_info.config",
                slice_info if slice_info is not None
                else slice_info_xml([{"index": n, "objects": names} for n in order]),
            )
        for name, data in (extra_members or {}).items():
            z.writestr(name, data)
    return path
