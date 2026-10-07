from station.matching import (
    EXACT,
    NONE,
    NORMALIZED,
    is_calibration_job,
    match_confidence,
    normalize_name,
    strip_ext,
)


def test_strip_ext_handles_every_known_extension():
    assert strip_ext("a.gcode.3mf") == "a"
    assert strip_ext("a.3mf.gcode") == "a"
    assert strip_ext("a.3mf") == "a"
    assert strip_ext("a.gcode") == "a"
    assert strip_ext("A.GCODE.3MF") == "A"
    assert strip_ext("plain") == "plain"
    assert strip_ext(None) == ""


def test_strip_ext_prefers_longest_extension():
    # ".gcode.3mf" must be removed whole, not just ".3mf".
    assert strip_ext("benchy.gcode.3mf") == "benchy"


def test_normalize_name_ignores_case_and_punctuation():
    assert normalize_name("ab123_My Part-v2.gcode.3mf") == "ab123mypartv2"
    assert normalize_name(None) == ""


def test_calibration_jobs_detected():
    assert is_calibration_job("auto_hms_check_for_user.gcode")
    assert is_calibration_job("AUTO_HMS_x")
    assert not is_calibration_job("benchy")
    assert not is_calibration_job(None)


def test_match_exact():
    assert match_confidence("ab123_benchy", "ab123_benchy.gcode.3mf") == EXACT


def test_match_normalized():
    assert match_confidence("ab123 Benchy", "ab123_benchy.gcode.3mf") == NORMALIZED


def test_match_none():
    assert match_confidence("something_else", "ab123_benchy.gcode.3mf") == NONE
    assert match_confidence(None, "x.gcode.3mf") == NONE
    assert match_confidence("x", None) == NONE


def test_calibration_never_matches():
    assert match_confidence("auto_hms_check_for_user.gcode", "auto_hms_check_for_user.gcode.3mf") == NONE
