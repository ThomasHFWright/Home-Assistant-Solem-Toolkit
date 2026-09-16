# Home Assistant Solem Toolkit Integration

## Bluetooth connection cleanup

The `fix/bluetooth-connection-cleanup` branch includes the station-discovery
changes below. Commands, metadata reads, and connection checks share a device
lock until cleanup completes. Notification cleanup has a two-second timeout;
disconnect has a five-second timeout and one retry, with connection-state
verification. Cancellation waits for cleanup, and release failures are logged
without replaying an acknowledged watering command.

This improves connection release for phone access. It does not add background
Bluetooth polling or guarantee simultaneous connections with the phone app.

## Experimental station discovery

The `experiment/station-discovery` branch adds `solem_toolkit.read_metadata`.
It sends only identification (`0f00`) and output-name (`3500`) read requests,
returning stored names, firmware, station count/source and raw response frames.

Station count interpretation is experimental: recognized V5 identification
profiles use the output-count byte, checked against the returned names. Unknown
or conflicting profiles return no count so callers can retain manual settings.
Unused output slots are never counted as stations. Incomplete name fragments
raise an error. Pair with the Controller's matching experimental branch.

## Controller acknowledgement patch

BLE commands now wait for a full status reply and final acknowledgement before
disconnecting. Missing or unrelated replies fail; starts and stops also verify
the reported active station. Command/status exchanges share a per-controller
lock. Connection attempts may retry, but uncertain writes are never replayed.

`solem_toolkit.read_status` accepts `device_mac` and optional `bluetooth_timeout`,
and returns `controller_on`, `active_station`, and `raw_notification` without
starting watering. These are controller snapshots, not flow measurements.

Parsing follows the [BL-IP V5 protocol documentation](https://github.com/beelzetron/solem-blip-ble/blob/main/docs/ble_protocol.md)
with unsupported response formats failing explicitly. Run the mocked regression
suite on Python 3.14:

```sh
python -m pip install -r requirements-test.txt
python -m pytest -q
```

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/hcraveiro/Home-Assistant-Solem-Toolkit.svg)](https://github.com/hcraveiro/Home-Assistant-Solem-Toolkit/releases/)

Integrate Solem Watering Bluetooth Controllers (only tested in BL-IP) into your Home Assistant. This Integration is meant to only provide services for Home Assistant to control irrigation using your BL-IP controller. 

- [Home Assistant Solem Toolkit Integration](#home-assistant-solem-toolkit-integration)
    - [Installation](#installation)
    - [Services](#services)
    - [FAQ](#faq)
    - [Credits](#credits)

## Installation

This integration can be added as a custom repository in HACS and from there you can install it.

When the integration is installed in HACS, you need to put on configuration.yaml:

```yaml
solem_toolkit:
```
Then you can restart Home Assistant and the services from Solem Toolkit will be available.

## Services

There is no configuration, you only need to use the provided services. They are self-explanatory:
* list_characteristics - List the services and its characteristics
* turn_off_permanent - Turn off the Sprinkler permanently
* turn_off_x_days - Turn off the Sprinkler for X days
* turn_on - Turn the Sprinkler on
* sprinkle_station_x_for_y_minutes - Sprinkle station X (number starting from 1) for Y minutes (integer)
* sprinkle_all_stations_for_y_minutes - Sprinkle all stations for Y minutes (integer)
* run_program_x - Run program X
* stop_manual_sprinkle - Stop the Sprinkler if it is sprinkling

## FAQ

### Can I configure the MAC address of the controller?

No, as this is 'just' a toolkit you need to provide it to every service. I plan to have a different Integration that will use this toolkit that will take care of that.

## Credits

A big thank you to [pcman75](https://github.com/pcman75) for doing a [reverse engineering](https://github.com/pcman75/solem-blip-reverse-engineering) on Solem controllers which helped me a lot. 
