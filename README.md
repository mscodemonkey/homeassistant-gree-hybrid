# Gree Hybrid for Home Assistant

[![My Home Assistant](https://img.shields.io/badge/Home%20Assistant-%2341BDF5.svg?style=flat&logo=home-assistant&label=My)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mscodemonkey&repository=homeassistant-gree-hybrid&category=integration)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](https://github.com/mscodemonkey/homeassistant-gree-hybrid/blob/main/LICENSE)

<p align="center">
  <img src="https://raw.githubusercontent.com/mscodemonkey/homeassistant-gree-hybrid/main/brand/icon@2x.png" width="160" alt="Gree Hybrid icon">
</p>

Gree Hybrid puts local and cloud-only Gree air conditioners into one Home
Assistant integration. It discovers the units in your Gree+ home, checks which
ones answer on the local network, and chooses the best connection for each
unit.

- LAN-capable units use local UDP control.
- Newer modules that do not expose the local protocol use Gree's regional MQTT
  service.
- If a unit appears in both places, local control wins.

Each device exposes a native Home Assistant `climate` entity, along with the
switches and sensors that the unit reports. These entities work with Home
Assistant climate cards and can be exposed through HomeKit Bridge or Alexa.
The `transport` state attribute shows whether a unit is using `local` or
`cloud`.

## Why this integration exists

Home Assistant's built-in Gree integration works well with older Wi-Fi modules
that answer on UDP port 7000. Some newer Gree modules stopped exposing that
local service, even though the same units still work in Gree+.

Running a separate local integration and cloud integration creates duplicate
devices and inconsistent entity names. Gree Hybrid uses the Gree+ account as
the device list, matches LAN responses by MAC address, and presents one device
per air conditioner.

We chose a standalone repository instead of forking an existing project for two
reasons. First, the integration needs a different boundary: local discovery,
cloud discovery, routing, and Home Assistant entities belong in one maintained
package. Second, we did not want runtime behaviour to depend on the release
state of a separate Gree protocol library. All Gree-specific network code lives
in `custom_components/gree_hybrid/protocol` and ships with the integration.

The Home Assistant entity layer began from the MIT-licensed
[`homeassistant-gree-cloud`](https://github.com/davo22/homeassistant-gree-cloud)
project. The protocol layer in this repository is self-contained and has no
Gree-specific Python dependency.

## Before you configure it

> [!WARNING]
> Do not use the same Gree+ username and password that you use in the mobile
> app. Gree+ now permits only one active login for an account, so Home Assistant
> can sign the app out and the app can interrupt Home Assistant.

Create a separate account for Home Assistant instead:

1. Open your home in the Gree+ app with your usual account.
2. Use **Invite members** to invite a different email address.
3. Accept the invitation and create a unique password for that account.
4. Use the invited member's email address and password in Home Assistant.

The invited account must be able to see the home and all air conditioners you
want Home Assistant to control.

## Install with HACS

Until this repository is included in HACS by default, add it as a custom
repository:

1. Open HACS in Home Assistant.
2. Select **Integrations**.
3. Open the menu and select **Custom repositories**.
4. Enter `https://github.com/mscodemonkey/homeassistant-gree-hybrid`.
5. Choose **Integration** as the category, then select **Add**.
6. Find **Gree Hybrid** in HACS and select **Download**.
7. Restart Home Assistant.

For a manual installation, copy `custom_components/gree_hybrid` into the
`custom_components` directory in your Home Assistant configuration and restart
Home Assistant.

## Configure Home Assistant

1. Go to **Settings > Devices & services**.
2. Select **Add integration**.
3. Search for **Gree Hybrid**.
4. Choose the same region used by your Gree+ home.
5. Enter the invited member account created above.

The integration imports the devices visible to that account and assigns a
transport at startup. Reload the integration if you add or remove an air
conditioner, change its network, or want it to repeat local discovery.

## What is supported

- Power and HVAC mode
- Target and current temperature
- Fan speed
- Horizontal and vertical swing
- Sleep, turbo, eco, away, quiet, panel light, fresh air, XFan, and health mode
  where the unit reports them
- Humidity, cumulative energy, and compressor frequency where available
- Gree hot-water heat pumps reported by the account
- Celsius, Fahrenheit, and half-degree setpoints where supported by the unit

Not every Gree model implements every property. Home Assistant only creates
optional sensor entities when the device returns usable data.

## How routing works

At startup the integration:

1. Signs in to the selected Gree+ region and reads the account's homes and
   devices.
2. Broadcasts a local Gree discovery packet on Home Assistant's IPv4 networks.
3. Matches cloud and LAN records by MAC address.
4. Tries a local bind for each matching unit.
5. Falls back to Gree cloud MQTT if local binding fails or no LAN device was
   found.

Local units are polled every 10 seconds. Cloud units are polled every 60
seconds. Reloading the integration repeats the selection. Automatic switching
between local and cloud after startup is not implemented yet.

## Troubleshooting

If all units use cloud, check that Home Assistant and the air conditioners are
on networks that permit UDP broadcast traffic and direct UDP traffic to port
7000. VLAN and guest-network isolation commonly block local discovery.

If no devices load, confirm that the invited Gree+ member can see the home in
the mobile app and that Home Assistant uses the matching region. Do not test by
signing the app into the Home Assistant account while the integration is
running, since that may invalidate Home Assistant's session.

Look at an entity's `transport` attribute in Developer Tools to confirm its
selected route. Integration logs also record one routing decision per device.

## Development

The repository uses Python 3.12 or newer and `uv` for its local checks:

```sh
uv sync
uv run ruff check .
uv run pytest -q
```

The protocol tests include fixed encryption vectors, authenticated GCM packet
checks, device-state handling, command ordering, and MQTT response handling.

## Security and status

Gree does not publish or support the local or cloud protocol used here. This is
an independent project and is not affiliated with Gree Electric Appliances.
Cloud control can stop working if Gree changes its private service. Local
control remains independent of Gree's servers for units that support it.

Home Assistant stores the invited account credentials in its config entry.
Use a unique password and do not reuse credentials from any other service.

## License

MIT. See [LICENSE](LICENSE).
