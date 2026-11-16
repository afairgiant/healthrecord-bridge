from __future__ import annotations

import pytest

from healthrecord_bridge.ccda.datetime_utils import normalize_offset, parse_ts, to_instant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2019", "2019"),
        ("201909", "2019-09"),
        ("20190920", "2019-09-20"),
        ("20190920165128-0400", "2019-09-20T16:51:28-04:00"),
        ("20190920165128+0000", "2019-09-20T16:51:28+00:00"),
        ("201909201651+0530", "2019-09-20T16:51:00+05:30"),
        ("2019092016-0500", "2019-09-20T16:00:00-05:00"),
        ("20190920165128.123-0400", "2019-09-20T16:51:28.123-04:00"),
        ("20190920165128.123456-0400", "2019-09-20T16:51:28.123-04:00"),
    ],
)
def test_parse_with_offset(raw, expected):
    parsed = parse_ts(raw)
    assert parsed is not None
    assert parsed.value == expected
    assert parsed.truncated is False


def test_time_without_offset_is_truncated_unless_assumed():
    parsed = parse_ts("20190920165128")
    assert parsed is not None
    assert parsed.value == "2019-09-20"
    assert parsed.has_time and parsed.truncated
    assumed = parse_ts("20190920165128", "Z")
    assert assumed is not None
    assert assumed.value == "2019-09-20T16:51:28Z"
    assert not assumed.truncated


@pytest.mark.parametrize(
    "raw", ["", None, "abc", "20191399", "20190931abc", "0000", "20190920251200"]
)
def test_garbage(raw):
    assert parse_ts(raw) is None


def test_to_instant_pads():
    assert to_instant("2019") == "2019-01-01T00:00:00Z"
    assert to_instant("201909", "-05:00") == "2019-09-01T00:00:00-05:00"
    assert to_instant("20190920165128") == "2019-09-20T16:51:28Z"
    assert to_instant("20190920165128-0400") == "2019-09-20T16:51:28-04:00"
    assert to_instant(None) is None


def test_normalize_offset():
    assert normalize_offset(None) is None
    assert normalize_offset("z") == "Z"
    assert normalize_offset("-0500") == "-05:00"
    assert normalize_offset("+05:30") == "+05:30"
    with pytest.raises(ValueError):
        normalize_offset("EST")


@pytest.mark.parametrize(
    "raw",
    [
        "20240231",  # February 31
        "20230229",  # not a leap year
        "20240431",  # April has 30 days
    ],
)
def test_impossible_day_keeps_the_month(raw):
    parsed = parse_ts(raw)
    assert parsed.value == raw[:4] + "-" + raw[4:6]
    assert parsed.invalid and not parsed.has_time


def test_impossible_day_drops_the_time_too():
    parsed = parse_ts("20240231120000-0500")
    assert parsed.value == "2024-02" and parsed.invalid


@pytest.mark.parametrize(
    "raw",
    [
        "20240301120000+9999",
        "20240301120000+1500",
        "20240301120000+1430",  # FHIR allows 14:00 exactly, nothing past it
        "20240301120000-0560",
    ],
)
def test_impossible_offset_keeps_the_date(raw):
    parsed = parse_ts(raw)
    assert parsed.value == "2024-03-01"
    assert parsed.invalid
    # Not "truncated": assume_timezone cannot recover a time whose offset was impossible.
    assert not parsed.truncated
    assert parse_ts(raw, "-05:00").value == "2024-03-01"
    assert parse_ts("19800515120000+9999").value == "1980-05-15"  # a birthTime keeps its date


@pytest.mark.parametrize("offset", ["+15:00", "+09:99", "+1430", "-1401"])
def test_assumed_timezone_must_be_a_fhir_offset(offset):
    with pytest.raises(ValueError):
        normalize_offset(offset)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20240229", "2024-02-29"),
        ("20240301120000+1400", "2024-03-01T12:00:00+14:00"),
        ("20240301120000-1400", "2024-03-01T12:00:00-14:00"),
        ("20240301120000+1345", "2024-03-01T12:00:00+13:45"),
    ],
)
def test_edge_dates_and_offsets_are_kept(raw, expected):
    assert parse_ts(raw).value == expected
