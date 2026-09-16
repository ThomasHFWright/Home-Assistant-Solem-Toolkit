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


IDENTITY = bytes.fromhex("100f01aabbccddeeffe206410501070100")


def test_identification_count_does_not_count_unused_or_unnamed_slots():
    frames = sum((name_frames(i, "Garden" if i == 1 else "") for i in range(1, 13)), [])
    result = parse_metadata([IDENTITY], frames)
    assert result["station_count"] == 6
    assert result["station_count_source"] == "v5_identification_experimental"
    assert result["firmware"] == "5.1.7"


@pytest.mark.parametrize("identity, slots, tail", [
    (IDENTITY[:9] + b"\x00" + IDENTITY[10:], 12, ""),
    (IDENTITY, 4, ""),
    (IDENTITY, 12, "Unexpected active output"),
])
def test_unknown_or_conflicting_identification_retains_manual_count(identity, slots, tail):
    frames = sum((name_frames(i, tail if i == 12 else "") for i in range(1, slots + 1)), [])
    assert parse_metadata([identity], frames)["station_count"] is None


async def test_metadata_reads_never_send_watering_or_commit(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from custom_components.solem_toolkit import api as module

    monkeypatch.setattr(module, "_NOTIFICATION_SETTLE_DELAY", 0)
    monkeypatch.setattr(module, "_METADATA_IDLE_TIMEOUT", 0.001)
    client = SimpleNamespace(is_connected=True, stop_notify=AsyncMock(), disconnect=AsyncMock())

    async def subscribe(uuid, callback):
        client.notify = lambda frame: callback(None, bytearray(frame))

    async def write(uuid, payload, **kwargs):
        if payload == b"\x0f\x00":
            client.notify(IDENTITY)
        elif payload == b"\x35\x00":
            for station in range(1, 13):
                for frame in name_frames(station, "Garden" if station == 1 else ""):
                    client.notify(frame)
        else:
            pytest.fail(f"Unexpected write: {payload.hex()}")

    client.start_notify = AsyncMock(side_effect=subscribe)
    client.write_gatt_char = AsyncMock(side_effect=write)
    api = module.SolemAPI(SimpleNamespace(data={}), "AA:BB:CC:DD:EE:FF")
    api._connect_client = AsyncMock(return_value=client)
    assert (await api.read_metadata())["station_count"] == 6
    assert [c.args[1] for c in client.write_gatt_char.await_args_list] == [b"\x0f\x00", b"\x35\x00"]
    assert client.disconnect.await_count == client.stop_notify.await_count == 2


@pytest.mark.parametrize("length", [2, 4, 19, 21])
def test_malformed_name_frame_cannot_replace_a_complete_name(length):
    frames = name_frames(1, "Existing garden")
    # Also reject a bad duplicate instead of overwriting a valid fragment.
    frames.append((frames[0] + b"x")[:length])
    with pytest.raises(APIConnectionError, match="Incomplete"):
        parse_metadata([], frames)


@pytest.mark.parametrize("has_response", [True, False], ids=["complete-response", "noise-only"])
async def test_unrelated_notifications_do_not_extend_metadata_wait(monkeypatch, has_response):
    import asyncio
    from contextlib import suppress
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from custom_components.solem_toolkit import api as module

    monkeypatch.setattr(module, "_NOTIFICATION_SETTLE_DELAY", 0)
    monkeypatch.setattr(module, "_METADATA_IDLE_TIMEOUT", 0.01)
    client = SimpleNamespace(is_connected=True, stop_notify=AsyncMock(), disconnect=AsyncMock())
    expected = name_frames(1, "Garden")

    async def subscribe(uuid, callback):
        client.notify = lambda frame: callback(None, bytearray(frame))

    async def send_noise():
        while True:
            await asyncio.sleep(0.001)
            client.notify(b"\x32unrelated")

    async def write(*args, **kwargs):
        if has_response:
            for frame in expected:
                client.notify(frame)
        client.noise = asyncio.create_task(send_noise())

    client.start_notify = AsyncMock(side_effect=subscribe)
    client.write_gatt_char = AsyncMock(side_effect=write)
    api = module.SolemAPI(SimpleNamespace(data={}), "AA:BB:CC:DD:EE:FF", bluetooth_timeout=0.1)
    api._connect_client = AsyncMock(return_value=client)
    try:
        if has_response:
            assert await api._read_metadata_frames(b"\x35\x00", b"\x36\x12") == expected
        else:
            with pytest.raises(APIConnectionError) as error:
                await api._read_metadata_frames(b"\x35\x00", b"\x36\x12")
            assert isinstance(error.value.__cause__, TimeoutError)
    finally:
        client.noise.cancel()
        with suppress(asyncio.CancelledError):
            await client.noise
    client.disconnect.assert_awaited_once()
    client.stop_notify.assert_awaited_once()
    assert not api._command_lock.locked()
