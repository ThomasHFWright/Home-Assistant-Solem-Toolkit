"""Solem BLE API helper.

This is a lightweight subset of the Solem API used by the scheduling integration.
It focuses on robust BLE connection handling and command writes for manual actions.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError
from bleak_retry_connector import (
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant

from .const import CHARACTERISTIC_UUID, DEFAULT_BLUETOOTH_TIMEOUT, NOTIFICATION_UUID

_LOGGER = logging.getLogger(__name__)
_COMMAND_LOCKS = "solem_toolkit_command_locks"
_NOTIFICATION_SETTLE_DELAY = 2.0
_METADATA_IDLE_TIMEOUT = 1.0


class APIConnectionError(Exception):
    """Exception raised when a BLE connection or write fails."""


class SolemAPI:
    """API wrapper for the Solem BLE protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        bluetooth_timeout: int = DEFAULT_BLUETOOTH_TIMEOUT,
    ) -> None:
        self.hass = hass
        self.mac_address = mac_address
        self.bluetooth_timeout = bluetooth_timeout

        self.characteristic_uuid: str = CHARACTERISTIC_UUID
        self._conn_lock = asyncio.Lock()
        locks = hass.data.setdefault(_COMMAND_LOCKS, {})
        self._command_lock = locks.setdefault((mac_address or "").upper(), asyncio.Lock())

    async def scan_bluetooth(self) -> list[BLEDevice]:
        """Return a list of discovered BLE devices."""
        return await BleakScanner.discover(timeout=5.0)

    async def _resolve_ble_device(self) -> BLEDevice:
        """Resolve a BLEDevice for the configured MAC address."""
        # First attempt: prefer Home Assistant-managed scanners so Bluetooth proxies are supported
        ble_device = async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )
        if ble_device is not None:
            return ble_device

        # Second attempt: direct lookup by address (fast-path on most platforms)
        ble_device = await BleakScanner.find_device_by_address(
            self.mac_address, timeout=5.0
        )
        if ble_device is not None:
            return ble_device

        # Fallback: full scan and manual match (some platforms/proxies behave like this)
        devices = await BleakScanner.discover(timeout=5.0)
        for d in devices:
            if (d.address or "").lower() == self.mac_address.lower():
                return d

        raise APIConnectionError("Device not found! Failed connecting!")

    async def _connect_client(self) -> BleakClient:
        """Establish a robust connection using bleak-retry-connector."""
        async with self._conn_lock:
            ble_device = await self._resolve_ble_device()
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    name=f"Solem - {self.mac_address}",
                    timeout=self.bluetooth_timeout,
                    max_attempts=3,
                )
                return client
            except BleakOutOfConnectionSlotsError as exc:
                raise APIConnectionError(
                    "Bluetooth adapter/proxy out of connection slots or device busy/unreachable"
                ) from exc
            except (BleakDBusError, TimeoutError, OSError) as exc:
                raise APIConnectionError("Timeout connecting to device") from exc
            except Exception as exc:  # noqa: BLE001
                raise APIConnectionError("Unexpected BLE connection error") from exc

    async def list_characteristics(self) -> dict:
        """Return discovered services/characteristics (debug helper)."""
        client = await self._connect_client()
        try:
            if not client.is_connected:
                raise APIConnectionError("Failed connecting!")

            # Home Assistant wraps BleakClient (HaBleakClientWrapper) and does not
            # expose BleakClient.get_services(). After connecting, discovered
            # services are available via the `services` attribute.
            services = getattr(client, "services", None)
            if services is None:
                # Last-resort fallback for non-HA clients / unexpected wrappers.
                inner = getattr(client, "_client", None) or getattr(client, "_bleak_client", None)
                if inner is not None and hasattr(inner, "get_services"):
                    services = await inner.get_services()
                else:
                    raise APIConnectionError("Services not available on this platform/client")
            result: dict = {}
            for svc in services:
                chars = []
                for c in svc.characteristics:
                    chars.append(
                        {
                            "uuid": str(c.uuid),
                            "properties": list(c.properties),
                            "descriptors": [str(d.uuid) for d in c.descriptors],
                        }
                    )
                result[str(svc.uuid)] = chars
            return result
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        """Write once: replaying an uncertain start can extend watering."""
        if not client.is_connected:
            raise APIConnectionError("Client not connected")

        await client.write_gatt_char(self.characteristic_uuid, payload, response=False)

    @asynccontextmanager
    async def _notification_session(
        self, client: BleakClient
    ) -> AsyncIterator[asyncio.Queue[bytes]]:
        """Subscribe to controller notifications for the duration of a command."""
        notifications: asyncio.Queue[bytes] = asyncio.Queue()

        def receive(sender, data):
            frame = bytes(data)
            _LOGGER.debug("%s - Notification: %s", self.mac_address, frame.hex())
            notifications.put_nowait(frame)

        try:
            await client.start_notify(NOTIFICATION_UUID, receive)
        except Exception as exc:  # noqa: BLE001
            raise APIConnectionError(
                f"Failed subscribing to controller notifications: {exc}"
            ) from exc

        try:
            yield notifications
        finally:
            with suppress(Exception):
                await client.stop_notify(NOTIFICATION_UUID)

    async def _wait_for_response(self, notifications: asyncio.Queue[bytes]) -> dict:
        """Wait for a BL-IP status frame and its final acknowledgement.

        V5 response families 0x32/0x3c use sequence 2 for full status,
        1 for intermediate data, and 0 for completion. Other notifications
        (including firmware metadata) must not satisfy a command.
        """
        status = None
        while True:
            frame = await notifications.get()
            if len(frame) < 3 or frame[0] not in (0x32, 0x3C):
                continue
            if len(frame) >= 18 and frame[1] == 0x10 and frame[2] == 2 and frame[3] != 0x10:
                status = frame
            elif frame[2] == 0 and status is not None and frame[0] == status[0]:
                return {
                    "controller_on": bool(status[3] & 0x40),
                    "active_station": status[9],
                    "raw_notification": status.hex(),
                }

    async def _exchange(self, command: bytes | None) -> dict:
        """Keep the BLE session open until the controller replies, or fail."""
        async with self._command_lock:
            client = await self._connect_client()
            try:
                async with self._notification_session(client) as notifications:
                    await asyncio.sleep(_NOTIFICATION_SETTLE_DELAY)
                    # Ignore any notifications received before this request.
                    while not notifications.empty():
                        notifications.get_nowait()
                    if command is not None:
                        _LOGGER.debug("%s - Sending command: %s", self.mac_address, command.hex())
                        await self._write(client, command)
                    # A bare commit is also the BL-IP status-poll request.
                    await self._write(client, b"\x3b\x00")
                    try:
                        response = await asyncio.wait_for(
                            self._wait_for_response(notifications), self.bluetooth_timeout
                        )
                    except TimeoutError as exc:
                        raise APIConnectionError(
                            "No complete controller acknowledgement after Bluetooth write; "
                            "device state is unconfirmed"
                        ) from exc
                    _LOGGER.info("%s - Controller response: %s", self.mac_address, response)
                    return response
            except APIConnectionError:
                raise
            except Exception as exc:
                raise APIConnectionError(f"Bluetooth command failed: {exc}") from exc
            finally:
                with suppress(Exception):
                    await client.disconnect()

    async def _write_and_commit(self, command: bytes) -> None:
        """Write once, commit, and wait for the controller's response."""
        response = await self._exchange(command)
        expected_station = None
        if command[2] == 0x12:
            expected_station = command[3]
        elif command[2] == 0x15:
            expected_station = 0
        if expected_station is not None and response["active_station"] != expected_station:
            raise APIConnectionError(
                f"Controller acknowledged the command but reports active station "
                f"{response['active_station']}; expected {expected_station}"
            )

    async def read_status(self) -> dict:
        """Read controller-reported state without issuing a watering command."""
        return await self._exchange(None)

    async def _read_metadata_frames(self, request: bytes, prefix: bytes) -> list[bytes]:
        """Collect a read response until idle, bounded by the operation timeout."""
        async with self._command_lock:
            client = await self._connect_client()
            try:
                async with self._notification_session(client) as notifications:
                    await asyncio.sleep(_NOTIFICATION_SETTLE_DELAY)
                    while not notifications.empty():
                        notifications.get_nowait()
                    await self._write(client, request)
                    frames = []
                    async with asyncio.timeout(self.bluetooth_timeout):
                        while True:
                            try:
                                frame = await asyncio.wait_for(
                                    notifications.get(), _METADATA_IDLE_TIMEOUT if frames else self.bluetooth_timeout
                                )
                            except TimeoutError:
                                if frames:
                                    return frames
                                raise
                            if frame.startswith(prefix):
                                frames.append(frame)
            except Exception as exc:
                raise APIConnectionError(f"Unable to read controller metadata: {exc}") from exc
            finally:
                with suppress(Exception):
                    await client.disconnect()

    async def read_metadata(self) -> dict:
        """Read identification and station names without sending a commit."""
        from .metadata import parse_metadata

        identity = await self._read_metadata_frames(b"\x0f\x00", b"\x10")
        names = await self._read_metadata_frames(b"\x35\x00", b"\x36\x12")
        return parse_metadata(identity, names)

    async def turn_on(self) -> None:
        """Turn on controller (enable watering)."""
        command = struct.pack(">HBBBH", 0x3105, 0xA0, 0x00, 0x01, 0x0000)
        await self._write_and_commit(command)

    async def turn_off_permanent(self) -> None:
        """Disable watering permanently."""
        command = struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, 0x00, 0x0000)
        await self._write_and_commit(command)

    async def turn_off_x_days(self, days: int) -> None:
        """Disable watering for X days."""
        days = max(0, min(days, 15))
        command = struct.pack(">HBBBH", 0x3105, 0xC0, 0x00, days, 0x0000)
        await self._write_and_commit(command)

    async def sprinkle_station_x_for_y_minutes(self, station: int, minutes: int) -> None:
        """Manually water a station for Y minutes."""
        station = max(1, min(station, 16))
        minutes = max(1, min(minutes, 720))
        seconds = minutes * 60
        command = struct.pack(">HBBBH", 0x3105, 0x12, station, 0x00, seconds)
        await self._write_and_commit(command)

    async def sprinkle_all_stations_for_y_minutes(self, minutes: int) -> None:
        """Manually water all stations for Y minutes each."""
        minutes = max(1, min(minutes, 720))
        seconds = minutes * 60
        command = struct.pack(">HBBBH", 0x3105, 0x11, 0x00, 0x00, seconds)
        await self._write_and_commit(command)

    async def run_program_x(self, program: int) -> None:
        """Run a controller program by id (1-3 on most devices)."""
        program = max(1, min(program, 3))
        command = struct.pack(">HBBBH", 0x3105, 0x14, 0x00, program, 0x0000)
        await self._write_and_commit(command)

    async def stop_manual_sprinkle(self) -> None:
        """Stop any running manual watering session."""
        command = struct.pack(">HBBBH", 0x3105, 0x15, 0x00, 0xFF, 0x0000)
        await self._write_and_commit(command)
