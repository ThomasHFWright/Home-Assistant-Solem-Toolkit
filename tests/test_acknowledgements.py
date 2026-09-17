"""Exercise actual protocol handling with a simulated BLE transport."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.solem_toolkit import api as module
from custom_components.solem_toolkit.api import APIConnectionError, SolemAPI

MAC = "AA:BB:CC:DD:EE:FF"
STATUS = bytes.fromhex("3210024200aaaaaa00014f0c10003c100000")
IDLE = bytes.fromhex("3c1002400000000000004f0c100000100000")
FINAL = bytes.fromhex("321000000000000000000000000000000000")
IDLE_FINAL = bytes.fromhex("3c1000000000000000000000000000000000")


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(module, "_NOTIFICATION_SETTLE_DELAY", 0)
    hass = SimpleNamespace(data={})
    client = SimpleNamespace(
        is_connected=True, stop_notify=AsyncMock(), disconnect=AsyncMock(),
        write_gatt_char=AsyncMock(), replies=(), commit_written=asyncio.Event(),
    )
    client.disconnect.side_effect = lambda: setattr(client, "is_connected", False)

    async def subscribe(uuid, callback):
        client.notify = lambda frame: callback(None, bytearray(frame))

    async def write(uuid, payload, **kwargs):
        if payload == b"\x3b\x00":
            client.commit_written.set()
            for frame in client.replies:
                client.notify(frame)

    client.write_gatt_char.side_effect = write
    client.start_notify = AsyncMock(side_effect=subscribe)
    api = SolemAPI(hass, MAC, bluetooth_timeout=0.03)
    api._connect_client = AsyncMock(return_value=client)
    return hass, api, client


async def test_start_waits_for_full_response_before_disconnect(transport):
    _, api, client = transport
    api.bluetooth_timeout = 1
    task = asyncio.create_task(api.sprinkle_station_x_for_y_minutes(1, 1))
    await client.commit_written.wait()
    client.notify(STATUS)
    await asyncio.sleep(0)
    assert not task.done()
    client.stop_notify.assert_not_awaited()
    client.disconnect.assert_not_awaited()
    client.notify(FINAL)
    await task
    assert [call.args[1] for call in client.write_gatt_char.await_args_list] == [
        bytes.fromhex("3105120100003c"), bytes.fromhex("3b00")
    ]
    client.stop_notify.assert_not_awaited()
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("frames", [[], [STATUS], [FINAL], [b"\x32"], [
    bytes.fromhex("100f01aabbccddeeffe206410501070100"),
    bytes.fromhex("10100044616c72696f000000000000000000"),
], [STATUS, IDLE_FINAL]])
async def test_missing_partial_unrelated_or_mismatched_reply_fails(transport, frames):
    _, api, client = transport
    client.replies = frames
    with pytest.raises(APIConnectionError, match="acknowledgement"):
        await api.sprinkle_station_x_for_y_minutes(1, 1)
    assert client.write_gatt_char.await_count == 2
    client.stop_notify.assert_not_awaited()
    client.disconnect.assert_awaited_once()


async def test_pre_request_notifications_do_not_satisfy_request(transport):
    _, api, client = transport
    original = client.start_notify.side_effect

    async def subscribe(uuid, callback):
        await original(uuid, callback)
        client.notify(STATUS)
        client.notify(FINAL)

    client.start_notify.side_effect = subscribe
    with pytest.raises(APIConnectionError, match="acknowledgement"):
        await api.sprinkle_station_x_for_y_minutes(1, 1)


async def test_read_status_sends_only_poll_and_returns_device_state(transport):
    _, api, client = transport
    client.replies = (IDLE, IDLE_FINAL)
    result = await api.read_status()
    assert result == {"controller_on": True, "active_station": 0, "raw_notification": IDLE.hex()}
    assert client.write_gatt_char.await_args.args[1] == b"\x3b\x00"
    assert client.write_gatt_char.await_count == 1


async def test_transport_error_preserved_and_start_not_replayed(transport):
    _, api, client = transport
    client.write_gatt_char.side_effect = OSError("proxy link lost")
    with pytest.raises(APIConnectionError, match="proxy link lost"):
        await api.sprinkle_station_x_for_y_minutes(1, 1)
    assert client.write_gatt_char.await_count == 1
    client.disconnect.assert_awaited_once()


async def test_cancellation_releases_connection_and_device_lock(transport):
    hass, api, client = transport
    api.bluetooth_timeout = 10
    task = asyncio.create_task(api.read_status())
    await client.commit_written.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.disconnect.assert_awaited_once()
    assert not api._command_lock.locked()
    assert SolemAPI(hass, MAC.lower())._command_lock is api._command_lock


async def test_concurrent_command_instances_wait_for_whole_exchange(transport):
    hass, api, client = transport
    api.bluetooth_timeout = 1
    first = asyncio.create_task(api.read_status())
    await client.commit_written.wait()
    other = SolemAPI(hass, MAC.lower(), bluetooth_timeout=0.01)
    other._connect_client = AsyncMock(side_effect=APIConnectionError("second connection"))
    second = asyncio.create_task(other.read_status())
    await asyncio.sleep(0)
    other._connect_client.assert_not_awaited()
    client.notify(IDLE)
    client.notify(IDLE_FINAL)
    await first
    with pytest.raises(APIConnectionError, match="second connection"):
        await second


async def test_acknowledged_start_reporting_idle_is_not_success(transport):
    _, api, client = transport
    client.replies = (IDLE, IDLE_FINAL)
    with pytest.raises(APIConnectionError, match="active station 0; expected 1"):
        await api.sprinkle_station_x_for_y_minutes(1, 1)


async def test_stop_frame_and_reported_stopped_state(transport):
    _, api, client = transport
    client.replies = (IDLE, IDLE_FINAL)
    await api.stop_manual_sprinkle()
    assert client.write_gatt_char.await_args_list[0].args[1] == bytes.fromhex("31051500ff0000")


async def test_acknowledged_stop_reporting_active_station_is_not_success(transport):
    _, api, client = transport
    client.replies = (STATUS, FINAL)
    with pytest.raises(APIConnectionError, match="active station 1; expected 0"):
        await api.stop_manual_sprinkle()
