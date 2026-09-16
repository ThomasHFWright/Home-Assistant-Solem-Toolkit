"""Synthetic metadata replies; no device-specific data."""
import pytest

from custom_components.solem_toolkit.api import APIConnectionError
from custom_components.solem_toolkit.metadata import parse_metadata


def name_frames(station, name):
    raw = name.encode()
    return [bytes([0x36, 0x12, seq, station - 1]) + part.ljust(16, b"\0")
            for seq, part in [(1, raw[:16]), (0, raw[16:])]]


def test_names_out_of_order_and_unused_slots():
    frames = name_frames(1, "Front garden east") + name_frames(12, "")
    result = parse_metadata([], frames[::-1])
    assert result["station_names"] == {"1": "Front garden east", "12": ""}
    assert result["station_count"] is None


@pytest.mark.parametrize("frames", [[], name_frames(1, "Garden")[:1]])
def test_incomplete_names_are_rejected(frames):
    with pytest.raises(APIConnectionError, match="Incomplete"):
        parse_metadata([], frames)


def test_utf8_can_span_fragments():
    assert parse_metadata([], name_frames(1, "a" * 15 + "é"))["station_names"]["1"] == "a" * 15 + "é"
