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
    monkeypatch.setattr(module, "_DISCONNECT_TIMEOUT", 0.01)
    client = SimpleNamespace(is_connected=True, services=[], start_notify=AsyncMock(),
                             stop_notify=AsyncMock(), write_gatt_char=AsyncMock())
    client.disconnect = AsyncMock(side_effect=lambda: setattr(client, "is_connected", False))
    api = SolemAPI(SimpleNamespace(data={}), "AA:BB:CC:DD:EE:FF", bluetooth_timeout=0.01)
    api._connect_client = AsyncMock(return_value=client)
    return api, client


async def hang(*args):
    await asyncio.Event().wait()


async def test_disconnect_preserves_original_write_failure(transport):
    api, client = transport
    client.write_gatt_char.side_effect = OSError("original write failure")
    with pytest.raises(APIConnectionError, match="original write failure"):
        await api.read_status()
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
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


async def test_disconnect_does_not_depend_on_explicit_notification_shutdown(transport):
    api, client = transport
    api._wait_for_response = AsyncMock(return_value={"active_station": 1})
    client.stop_notify.side_effect = asyncio.CancelledError
    await api.sprinkle_station_x_for_y_minutes(1, 1)
    assert not client.is_connected
    client.disconnect.assert_awaited_once()
    assert client.write_gatt_char.await_count == 2


async def test_connection_state_error_does_not_skip_disconnect_or_mask_reply(transport, caplog):
    from unittest.mock import Mock, PropertyMock

    api, _ = transport
    client = Mock(disconnect=AsyncMock(), start_notify=AsyncMock())
    type(client).is_connected = PropertyMock(side_effect=OSError("state unavailable"))
    api._connect_client.return_value = client
    api._write = AsyncMock()
    api._wait_for_response = AsyncMock(return_value={"active_station": 1})
    await api.sprinkle_station_x_for_y_minutes(1, 1)
    assert client.disconnect.await_count == 2
    assert "Bluetooth release could not be confirmed" in caplog.text
    assert api._write.await_count == 2
    assert not api._command_lock.locked()


@pytest.mark.parametrize("failure", [APIConnectionError("connect failed"), asyncio.CancelledError()])
async def test_failed_acquisition_releases_lock_without_a_client(transport, failure):
    api, client = transport
    api._connect_client.side_effect = failure
    with pytest.raises(type(failure)):
        await api.read_status()
    client.disconnect.assert_not_awaited()
    client.start_notify.assert_not_awaited()
    assert not api._command_lock.locked()
