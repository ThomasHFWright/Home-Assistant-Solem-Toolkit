"""Radio release under failures and cancellation, using a simulated BLE client."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.solem_toolkit import api as module
from custom_components.solem_toolkit.api import APIConnectionError, SolemAPI


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(module, "_NOTIFICATION_SETTLE_DELAY", 0)
    monkeypatch.setattr(module, "_NOTIFICATION_CLEANUP_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "_DISCONNECT_TIMEOUT", 0.01)
    client = SimpleNamespace(is_connected=True, services=[], start_notify=AsyncMock(),
                             stop_notify=AsyncMock(), write_gatt_char=AsyncMock())
    client.disconnect = AsyncMock(side_effect=lambda: setattr(client, "is_connected", False))
    api = SolemAPI(SimpleNamespace(data={}), "AA:BB:CC:DD:EE:FF", bluetooth_timeout=0.01)
    api._connect_client = AsyncMock(return_value=client)
    return api, client


async def hang(*args):
    await asyncio.Event().wait()


@pytest.mark.parametrize("failure", [hang, OSError("unsubscribe failed")])
async def test_notification_cleanup_cannot_prevent_disconnect(transport, failure, caplog):
    api, client = transport
    client.stop_notify.side_effect = failure
    client.write_gatt_char.side_effect = OSError("original write failure")
    with pytest.raises(APIConnectionError, match="original write failure"):
        await asyncio.wait_for(api.read_status(), 1)
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    assert "Notification cleanup failed" in caplog.text
    assert not api._command_lock.locked()


@pytest.mark.parametrize("failure", ["timeout", "exception", "still_connected"])
async def test_disconnect_retried_without_replaying_acknowledged_command(transport, failure, caplog):
    api, client = transport
    api._wait_for_response = AsyncMock(return_value={"active_station": 1})

    async def disconnect():
        if client.disconnect.await_count == 1:
            if failure == "timeout":
                await hang()
            if failure == "exception":
                raise OSError("proxy error")
            return
        client.is_connected = False

    client.disconnect.side_effect = disconnect
    await asyncio.wait_for(api.sprinkle_station_x_for_y_minutes(1, 1), 1)
    assert client.disconnect.await_count == 2
    assert not client.is_connected
    assert client.write_gatt_char.await_count == 2  # One start and one commit.
    assert "Disconnect attempt" in caplog.text or "still connected" in caplog.text


async def test_persistent_disconnect_failure_logged_without_masking_reply(transport, caplog):
    api, client = transport
    api._wait_for_response = AsyncMock(return_value={"active_station": 1})
    client.disconnect.side_effect = hang
    await asyncio.wait_for(api.sprinkle_station_x_for_y_minutes(1, 1), 1)
    assert client.disconnect.await_count == 2
    assert "Bluetooth release could not be confirmed" in caplog.text
    assert client.write_gatt_char.await_count == 2
    assert not api._command_lock.locked()


@pytest.mark.parametrize("operation", ["status", "metadata", "characteristics"])
async def test_cancellation_during_cleanup_keeps_lock_until_release(transport, monkeypatch, operation):
    api, client = transport
    monkeypatch.setattr(module, "_DISCONNECT_TIMEOUT", 1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def disconnect():
        entered.set()
        await release.wait()
        client.is_connected = False

    client.disconnect.side_effect = disconnect
    api._wait_for_response = AsyncMock(return_value={"active_station": 0})
    # Make metadata reach its finally block immediately with the original error.
    if operation == "metadata":
        client.write_gatt_char.side_effect = OSError("read failed")
    call = {"status": api.read_status, "metadata": api.read_metadata,
            "characteristics": api.list_characteristics}[operation]
    task = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 1)
    other = SolemAPI(api.hass, api.mac_address.lower())
    other._connect_client = AsyncMock(side_effect=APIConnectionError("next operation"))
    next_task = asyncio.create_task(other.list_characteristics())
    try:
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert api._command_lock.locked()
            other._connect_client.assert_not_awaited()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    with pytest.raises(APIConnectionError, match="next operation"):
        await asyncio.wait_for(next_task, 1)
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    assert not api._command_lock.locked()


async def test_subscription_failure_still_releases_connection(transport):
    api, client = transport
    client.start_notify.side_effect = OSError("subscribe failed")
    with pytest.raises(APIConnectionError, match="subscribe failed"):
        await api.read_status()
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    client.write_gatt_char.assert_not_awaited()


async def test_cancellation_during_subscription_still_releases_connection(transport):
    api, client = transport
    entered = asyncio.Event()

    async def subscribe(*args):
        entered.set()
        await hang()

    client.start_notify.side_effect = subscribe
    task = asyncio.create_task(api.read_status())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    assert not api._command_lock.locked()


async def test_cancellation_during_notification_cleanup_still_disconnects(transport):
    api, client = transport
    entered = asyncio.Event()
    api._wait_for_response = AsyncMock(return_value={"active_station": 0})

    async def unsubscribe(*args):
        entered.set()
        await hang()

    client.stop_notify.side_effect = unsubscribe
    task = asyncio.create_task(api.read_status())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    assert not api._command_lock.locked()
