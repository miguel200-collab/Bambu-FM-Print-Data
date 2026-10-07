import zipfile
from pathlib import Path

import pytest

from station import gcode_features as gf
from tests.helpers import build_3mf

REAL_SAMPLES = Path.home() / "Desktop" / "3mf_sample_data" / "MakerLAB_Multimaterial_Anchors"


# --------------------------------------------------------------------------- pure parsers

@pytest.mark.parametrize("text,seconds", [
    ("1h 6m 55s", 4015.0),
    ("58m 16s", 3496.0),
    ("2d 3h", 183600.0),
    ("45s", 45.0),
    ("", None),
    (None, None),
    ("soon", None),
])
def test_parse_duration(text, seconds):
    assert gf.parse_duration(text) == seconds


def test_numeric_parsers_handle_multi_value_and_percent():
    assert gf.to_floats("220,220") == [220.0, 220.0]
    assert gf.to_floats("15%") == [15.0]
    assert gf.to_floats("a,3,b") == [3.0]
    assert gf.first_number("220,230") == 220.0
    assert gf.first_number("15%") == 15.0
    assert gf.first_number("") is None
    assert gf.first_number(None) is None
    assert gf.first_int("5") == 5


def test_parse_header_lines_both_formats():
    out = gf.parse_header_lines([
        "; BambuStudio 02.03.00.70",
        "; model printing time: 58m 16s; total estimated time: 1h 6m 55s",
        "; total layer number: 240",
        "; total filament length [mm] : 4036.46",
        "; max_z_height: 48.00",
    ])
    assert out["slicer"] == "BambuStudio 02.03.00.70"
    assert out["model_printing_time"] == "58m 16s"
    assert out["total_estimated_time"] == "1h 6m 55s"
    assert out["total_layer_number"] == "240"
    assert out["total_filament_length"] == "4036.46"     # unit in [] is dropped from the key
    assert out["max_z_height"] == "48.00"


# --------------------------------------------------------------------------- extraction

def test_extracts_header_config_and_metadata(tmp_path):
    f = build_3mf(tmp_path / "a.gcode.3mf", names=["Bracket"], area=454.4, dims=(39.0, 16.2))
    r = gf.extract_features(f)
    c = r["curated"]

    assert r["is_sliced"] and r["error"] is None
    assert r["plate_count"] == 1 and r["plate_index"] == 1
    assert r["object_names"] == ["Bracket"]
    # header values the old extract.py silently dropped
    assert c["layer_count"] == 2
    assert c["max_z_mm"] == 48.0
    assert c["model_time_s"] == 3496.0
    assert c["filament_mm"] == pytest.approx(4036.46)
    # slice_info beats the header for time / weight
    assert c["est_time_s"] == 3371.0
    assert c["filament_g"] == pytest.approx(9.61)
    assert c["filament_type"] == "PLA" and c["filament_count"] == 1
    assert c["support_used"] == 0 and c["printer_model_id"] == "BL-P001"
    # plate json
    assert c["footprint_area_mm2"] == pytest.approx(454.4)
    assert c["bbox_x_mm"] == pytest.approx(39.0) and c["bbox_y_mm"] == pytest.approx(16.2)
    assert c["object_count"] == 1
    assert c["nozzle_diameter"] == 0.4                       # float noise rounded away
    assert c["first_layer_time_s"] == 58.5
    # config: "15%" and "220,220" parsed, raw strings kept
    assert c["infill_density"] == 15.0
    assert c["nozzle_temp"] == 220.0
    assert r["config"]["nozzle_temperature"] == "220,220"
    assert c["wall_loops"] == 2 and c["top_shell_layers"] == 5
    assert c["infill_pattern"] == "grid" and c["bed_type"] == "Textured PEI Plate"
    assert c["bed_temp"] == 55.0                             # textured plate temp, not hot_plate
    assert c["slicer"] == "BambuStudio 02.03.00.70"
    assert r["gcode_md5"] and len(r["gcode_md5"]) == 32 and r["gcode_md5"] == r["gcode_md5"].lower()


def test_gcode_statistics_match_the_known_toolpath(tmp_path):
    r = gf.extract_features(build_3mf(tmp_path / "a.gcode.3mf"))
    s, c = r["stats"], r["curated"]
    assert s["layer_count"] == 2
    assert s["retraction_count"] == c["retraction_count"] == 1
    assert s["extrusion_mm"] == c["extrusion_mm"] == pytest.approx(20.0)
    assert s["travel_mm"] == c["travel_mm"] == pytest.approx(20.0)
    assert c["travel_extrusion_ratio"] == pytest.approx(1.0)
    assert c["bridge_feature_count"] == 1
    assert c["overhang_feature_count"] == 1
    assert c["support_feature_count"] == 0
    assert s["feature_counts"] == {"Outer wall": 1, "Bridge": 1, "Overhang wall": 1}


@pytest.mark.parametrize("bed,temp_key,expected", [
    ("Textured PEI Plate", "textured_plate_temp", 55.0),
    ("Cool Plate", "cool_plate_temp", 35.0),
    ("Smooth PEI Plate / High Temp Plate", "hot_plate_temp", 60.0),
])
def test_bed_temp_follows_the_plate_type(tmp_path, bed, temp_key, expected):
    f = build_3mf(tmp_path / "a.gcode.3mf", config_overrides={"curr_bed_type": bed})
    assert gf.extract_features(f)["curated"]["bed_temp"] == expected


def test_plain_3mf_model_is_not_sliced_and_does_not_raise(tmp_path):
    f = build_3mf(tmp_path / "model.3mf", sliced=False)
    r = gf.extract_features(f)
    assert r["is_sliced"] is False
    assert r["error"] is None
    assert all(v is None for v in r["curated"].values())


@pytest.mark.parametrize("content", [b"", b"this is plain text, not a zip", b"PK\x03\x04truncated"])
def test_garbage_raises_a_clean_error(tmp_path, content):
    f = tmp_path / "bad.gcode.3mf"
    f.write_bytes(content)
    with pytest.raises(gf.FeatureError):
        gf.extract_features(f)


def test_missing_file_raises_clean_error(tmp_path):
    with pytest.raises(gf.FeatureError):
        gf.extract_features(tmp_path / "nope.gcode.3mf")


def test_multi_plate_uses_the_plate_listed_in_slice_info(tmp_path):
    f = build_3mf(tmp_path / "multi.gcode.3mf", plates=3, listed_first=2)
    r = gf.extract_features(f)
    assert r["plate_count"] == 3
    assert r["plate_index"] == 2
    # the footprint comes from THAT plate's json (x0 = 100 + 10 * plate)
    assert r["plate_json"]["bbox_all"][0] == 120.0


def test_multi_plate_defaults_to_lowest_plate_without_slice_info(tmp_path):
    f = build_3mf(tmp_path / "multi.gcode.3mf", plates=2, slice_info="<config/>")
    assert gf.extract_features(f)["plate_index"] == 1


def test_xml_with_entities_is_refused_but_extraction_still_works(tmp_path):
    bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><config><plate>'
            '<metadata key="index" value="1"/><metadata key="prediction" value="&a;"/></plate></config>')
    r = gf.extract_features(build_3mf(tmp_path / "x.gcode.3mf", slice_info=bomb))
    assert r["is_sliced"]
    assert r["object_names"] == []                      # slice_info ignored
    assert r["curated"]["est_time_s"] == 4015.0         # fell back to the header's "1h 6m 55s"


def test_malformed_xml_is_tolerated(tmp_path):
    r = gf.extract_features(build_3mf(tmp_path / "x.gcode.3mf", slice_info="<config><plate>"))
    assert r["is_sliced"] and r["curated"]["layer_count"] == 2


def test_oversized_metadata_members_are_ignored(tmp_path, monkeypatch):
    f = build_3mf(tmp_path / "x.gcode.3mf")
    monkeypatch.setattr(gf, "MAX_SMALL_MEMBER_BYTES", 10)
    r = gf.extract_features(f)
    assert r["object_names"] == [] and r["gcode_md5"] is None
    assert r["curated"]["footprint_area_mm2"] is None   # plate json skipped too
    assert r["curated"]["layer_count"] == 2             # the G-code itself is still read


def test_huge_gcode_skips_the_statistics_pass_but_keeps_header_and_config(tmp_path, monkeypatch):
    f = build_3mf(tmp_path / "x.gcode.3mf")
    monkeypatch.setattr(gf, "MAX_GCODE_STATS_BYTES", 1)
    r = gf.extract_features(f)
    assert r["stats"]["scanned"] is False
    assert r["curated"]["retraction_count"] is None
    assert r["curated"]["infill_density"] == 15.0
    assert r["curated"]["layer_count"] == 2             # from the header


def test_gcode_without_header_or_config_blocks_still_gets_statistics(tmp_path):
    f = build_3mf(tmp_path / "x.gcode.3mf", blocks=False)
    r = gf.extract_features(f)
    assert r["is_sliced"]
    assert r["config"] == {}
    assert r["stats"]["config_source"] is None
    assert r["curated"]["extrusion_mm"] == pytest.approx(20.0)


def test_config_falls_back_to_project_settings_json(tmp_path):
    import json
    settings = json.dumps({"layer_height": "0.12", "nozzle_temperature": ["210", "210"], "wall_loops": "3"})
    f = build_3mf(tmp_path / "x.gcode.3mf", blocks=False,
                  extra_members={"Metadata/project_settings.config": settings.encode()})
    r = gf.extract_features(f)
    assert r["stats"]["config_source"] == "project_settings"
    assert r["curated"]["layer_height"] == 0.12
    assert r["curated"]["nozzle_temp"] == 210.0
    assert r["curated"]["wall_loops"] == 3


def test_non_utf8_bytes_in_gcode_do_not_crash(tmp_path):
    f = tmp_path / "x.gcode.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/plate_1.gcode", b"; HEADER_BLOCK_START\n; total layer number: 7\n"
                   b"; HEADER_BLOCK_END\n; caf\xe9 \xff\xfe\nG1 X5 Y0 E1\n")
    r = gf.extract_features(f)
    assert r["curated"]["layer_count"] == 7
    assert r["curated"]["extrusion_mm"] == pytest.approx(5.0)


def test_result_is_json_serialisable(tmp_path):
    import json
    json.dumps(gf.extract_features(build_3mf(tmp_path / "x.gcode.3mf")))


def test_sha256_file(tmp_path):
    import hashlib
    f = tmp_path / "f.bin"
    f.write_bytes(b"hello")
    assert gf.sha256_file(f) == hashlib.sha256(b"hello").hexdigest()


def test_failed_result_shape():
    r = gf.failed_result("boom")
    assert r["error"] == "boom" and r["is_sliced"] is False
    assert set(r["curated"]) == set(gf.CURATED_COLUMNS)


# --------------------------------------------------------------------------- real samples

@pytest.mark.skipif(not REAL_SAMPLES.exists(), reason="real Benchy samples not on this machine")
def test_real_benchy_samples():
    abs_ = gf.extract_features(REAL_SAMPLES / "BenchyABS.gcode.3mf")
    pla = gf.extract_features(REAL_SAMPLES / "BenchyGenericPLA.gcode.3mf")
    for r in (abs_, pla):
        assert r["is_sliced"] and r["curated"]["layer_count"] == 240
        assert r["curated"]["layer_count"] == r["stats"]["layer_count"]      # header == counted
        assert r["object_names"] == ["3DBenchy"]
        assert r["curated"]["footprint_area_mm2"] == pytest.approx(454.4, abs=0.1)
    assert abs_["curated"]["filament_type"] == "ABS" and pla["curated"]["filament_type"] == "PLA"
    assert abs_["curated"]["nozzle_temp"] == 270.0 and pla["curated"]["nozzle_temp"] == 220.0
    assert abs_["gcode_md5"] != pla["gcode_md5"]            # same model, different toolpath
    assert len(abs_["config"]) > 400
