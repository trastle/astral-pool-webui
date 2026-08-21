import datetime

from app import (
    badge,
    chlorine_level,
    dosing_level,
    fmt_time_of_day,
    fmt_timedelta_days,
    humanize,
    mode_level,
    ph_level,
)
from quirks import decode_pool_volume, format_time_of_day


def test_decode_pool_volume_decodes_little_endian_bytes():
    assert decode_pool_volume(b"p\x94\x00") == 38000


def test_decode_pool_volume_passes_through_non_bytes():
    assert decode_pool_volume(3000) == 3000


def test_format_time_of_day_formats_as_hh_mm_ss():
    assert format_time_of_day(datetime.timedelta(hours=10)) == "10:00:00"
    assert format_time_of_day(datetime.timedelta(hours=14, minutes=5, seconds=30)) == "14:05:30"
    assert format_time_of_day(datetime.timedelta(hours=0, minutes=0)) == "00:00:00"


def test_humanize_inserts_spaces_between_words():
    assert humanize("InhibitedIndefinitely") == "Inhibited Indefinitely"
    assert humanize("NoMessage") == "No Message"
    assert humanize("ManualOn") == "Manual On"


def test_humanize_uppercases_orp():
    assert humanize("OrpProbeCleanCalibrate") == "ORP Probe Clean Calibrate"


def test_humanize_leaves_single_words_alone():
    assert humanize("Auto") == "Auto"
    assert humanize("Ok") == "Ok"


def test_ph_level_thresholds():
    assert ph_level(7.4) == "ok"
    assert ph_level(7.2) == "ok"
    assert ph_level(7.8) == "ok"
    assert ph_level(6.9) == "warn"
    assert ph_level(8.1) == "warn"
    assert ph_level(8.8) == "alert"
    assert ph_level(5.0) == "alert"


def test_chlorine_level():
    assert chlorine_level("Ok") == "ok"
    assert chlorine_level("Low") == "warn"
    assert chlorine_level("High") == "warn"
    assert chlorine_level("VeryVeryLow") == "alert"
    assert chlorine_level("Unknown") == "alert"


def test_mode_level():
    assert mode_level("Auto") == "ok"
    assert mode_level("ManualOn") == "warn"
    assert mode_level("Off") == "alert"
    assert mode_level("SomethingUnexpected") == "neutral"


def test_dosing_level():
    assert dosing_level("NotInhibited") == "ok"
    assert dosing_level("InhibitedForAPeriod") == "warn"
    assert dosing_level("InhibitedIndefinitely") == "alert"


def test_fmt_timedelta_days_rounds_to_whole_days():
    assert fmt_timedelta_days(datetime.timedelta(days=238)) == "238 days"
    assert fmt_timedelta_days(datetime.timedelta(days=33, hours=12)) == "34 days"


def test_fmt_time_of_day_formats_as_hh_mm():
    assert fmt_time_of_day(datetime.timedelta(hours=10)) == "10:00"
    assert fmt_time_of_day(datetime.timedelta(hours=14, minutes=5)) == "14:05"
    assert fmt_time_of_day(datetime.timedelta(hours=0, minutes=0)) == "00:00"


def test_badge_renders_expected_html():
    assert badge("Auto", "ok") == "<span class='badge ok'>Auto</span>"
