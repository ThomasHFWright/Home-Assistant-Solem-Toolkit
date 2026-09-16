"""Read-only BL-IP metadata parsing (experimental)."""

from .api import APIConnectionError


def parse_metadata(identity: list[bytes], names: list[bytes]) -> dict:
    """Assemble names without mistaking unused output slots for real stations."""
    fragments: dict[int, dict[int, bytes]] = {}
    for frame in names:
        if len(frame) < 4 or frame[:2] != b"\x36\x12" or frame[3] >= 12:
            continue
        fragments.setdefault(frame[3] + 1, {})[frame[2] & 1] = frame[4:20].split(b"\0", 1)[0]
    if not fragments or any(parts.keys() != {0, 1} for parts in fragments.values()):
        raise APIConnectionError("Incomplete station-name response; existing names retained")
    station_names = {
        str(station): (parts[1] + parts[0]).decode("utf-8", errors="replace").strip()
        for station, parts in sorted(fragments.items())
    }
    info = next((f for f in identity if len(f) >= 15 and f[:3] == b"\x10\x0f\x01"), None)
    # Experimental V5 identification profile: E2 <outputs> 41, firmware 5.x.
    # Never derive the hardware count from the number of name frames: unused
    # slots are transmitted too. Unknown profiles retain manual configuration.
    count = None
    if (info and info[9] == 0xE2 and info[11] == 0x41 and info[12] == 5
            and info[10] in (1, 2, 4, 6, 8)):
        candidate = info[10]
        if all(str(i) in station_names for i in range(1, candidate + 1)) and not any(
            name for i, name in station_names.items() if int(i) > candidate
        ):
            count = candidate
    return {
        "station_count": count,
        "station_count_source": "v5_identification_experimental" if count else "unknown",
        "station_names": station_names,
        "firmware": ".".join(str(n) for n in info[12:15]) if info else None,
        "identification_frames": [f.hex() for f in identity],
        "name_frames": [f.hex() for f in names],
    }
