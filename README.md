# Mondial Relay Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-mondial-relay.svg)](https://github.com/ha-parcel-integrations/ha-mondial-relay/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-mondial-relay/total.svg)](https://github.com/ha-parcel-integrations/ha-mondial-relay/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

> ⚠️ **Pre-1.0 release.** Each parcel's status (in transit, ready at the
> pickup point, delivered) is mapped. Parcels followed by tracking code also
> show their expected delivery day, a tracking link, the status history, the
> merchant's code as sender and, once collected, the pickup point's name; on
> an account hub those are not populated yet. See [Parcel status reference](#parcel-status-reference)
> and [Troubleshooting](#troubleshooting).

A custom Home Assistant integration that tracks your [Mondial Relay](https://www.mondialrelay.fr) parcels. There are two ways to set it up, and you can use both side by side:

- **Tracking code** — follow parcels sent *to* you with the tracking code and the postcode on the label. No account needed. This is the way to follow a parcel that never shows up in your own Mondial Relay account, such as one bought on a second-hand marketplace.
- **Account** — sign in with your Mondial Relay / InPost Group account and every parcel it already knows about, sent and received, is imported automatically. There is nothing to type in per parcel.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Services](#services)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Tracking hubs** follow incoming parcels by tracking code and postcode, including parcels your own account never sees
- **Account hubs** auto-import every parcel your Mondial Relay account already knows about, both **incoming and outgoing** — no per-parcel setup
- Per-parcel sensor with the carrier's own status text (`raw_status`) and a barcode
- Summary sensors: incoming parcels, outgoing parcels (account hubs), next delivery, awaiting pickup, recently delivered
- Read-only **Deliveries** calendar, filled from the expected delivery day of parcels followed by tracking code
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, and the outgoing equivalents on account hubs)
- `track_parcel` / `untrack_parcel` services to add or remove a tracking code from an automation or dashboard
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- For a tracking hub: the parcel's tracking code and the postcode it is addressed to
- For an account hub: a Mondial Relay / InPost Group account, reachable through a browser during setup

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-mondial-relay` as an **Integration**.
3. Install **Mondial Relay** and restart Home Assistant if HACS asks you to.

Or click the button below to open it directly in HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-mondial-relay&category=integration)

### Manual

Copy `custom_components/mondial_relay` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mondial_relay)

Or add the integration by hand via **Settings → Devices & Services → Add Integration → Mondial Relay**. The first screen asks which kind of hub to set up.

### Tracking code

Choose **Tracking code** and enter the postcode the parcels are addressed to, written as on the label. That is all setup asks: nothing is looked up yet. Then open **Configure** on the new entry and add the tracking codes under **Parcels**, or call the [`track_parcel` service](#services). Every parcel in this hub is looked up with its postcode, so each one needs only its code.

- A household that receives parcels at two addresses adds one hub per postcode.
- A tracking hub follows parcels sent **to** you only. Parcels you send come from an account hub: the sender has no safe way to look a parcel up by the recipient's postcode.
- A parcel that is tracked in an account hub and in a tracking hub shows up twice, once in each.
- A code that does not match the postcode simply shows as `unknown` until it does. Check that the postcode is written exactly as on the label.

### Account

**Before you start:** sign in once on Mondial Relay's own website (or in their app) with the account you want to use, and make sure your phone number is confirmed there — [mondialrelay.fr](https://www.mondialrelay.fr), [mondialrelay.be](https://www.mondialrelay.be), [mondialrelay.nl](https://www.mondialrelay.nl), [inpost.es](https://www.inpost.es) or [inpost.pt](https://www.inpost.pt). Mondial Relay does not release an account's parcels until it has signed in there at least once, and Home Assistant cannot do that step for you.

Choose **Account**. Home Assistant cannot receive the sign-in page's redirect directly, so the flow works in three steps:

1. Pick the country your Mondial Relay account is registered in — France, Belgium, the Netherlands, Spain or Portugal. It decides which sign-in page you are sent to, and which phone number that page accepts.
2. Open the address the setup screen shows you in a browser and sign in with your Mondial Relay / InPost Group account. The page will not finish loading afterwards — that is expected.
3. Copy the full address from your browser's address bar and paste it back into the setup form.

Your password is never entered into Home Assistant; only Mondial Relay's own sign-in page sees it. Home Assistant stores the resulting sign-in token only.

## Options

Open **Configure** on the integration entry. A tracking hub offers two pages:

| Page | Option | Default | Description |
|---|---|---|---|
| Parcels | Tracking codes | none | The codes this hub follows. Changes apply straight away. |
| Settings | Delivered parcels: filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Settings | Include status history | off | Adds each parcel's tracking events to its sensor. |

An account hub has one form:

| Section | Option | Default | Description |
|---|---|---|---|
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Reserved for a future release — see the pre-1.0 note above. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Services

Both act on tracking hubs only; an account hub's parcels come from the account itself.

| Service | Fields | What it does |
|---|---|---|
| `mondial_relay.track_parcel` | `tracking_code`, optional `postal_code` | Starts following a code. With more than one tracking hub, `postal_code` picks the hub. |
| `mondial_relay.untrack_parcel` | `tracking_code`, optional `postal_code` | Stops following a code — in the hub for `postal_code`, or in every tracking hub when it is left out. |

```yaml
action: mondial_relay.track_parcel
data:
  tracking_code: "12345678"
  postal_code: "1000"
```

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way. Every
  parcel currently falls into this tier, since Mondial Relay reports no
  out-for-delivery step (see the pre-1.0 note above).
- **Tracking hubs stop when there is nothing left to follow** — with no
  codes, or every code delivered, a tracking hub stops polling until you add
  a code. A delivered code is not looked up again.
- **Account hubs never fully stop** — with nothing hot or in transit,
  polling keeps running at the normal cadence, since that's also how a new
  shipment on your account gets discovered.
- A small, fixed per-hub offset is added on top, so not every Mondial Relay
  hub out there polls at exactly the same second.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Mondial Relay → ⋮ → Delete**. Nothing is stored on Mondial Relay's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.mondial_relay_incoming_parcels` | Number of active parcels you are receiving, full list under the `parcels` attribute |
| `sensor.mondial_relay_outgoing_parcels` | Number of active parcels you sent (account hubs only) |
| `sensor.mondial_relay_parcel_<code>` | One per tracked parcel (either direction); state is the canonical status, attributes carry the full normalised parcel |
| `sensor.mondial_relay_next_delivery` | Earliest expected delivery moment across all active incoming parcels — on an account hub currently always empty, see the pre-1.0 note above |
| `sensor.mondial_relay_awaiting_pickup` | Number of incoming parcels ready to collect at a pickup point |
| `sensor.mondial_relay_delivered_parcels` | Recently delivered incoming parcels (see the retention option) |
| `sensor.mondial_relay_outgoing_delivered_parcels` | Recently delivered outgoing parcels (account hubs only; see the retention option) |
| `sensor.mondial_relay_last_successful_update` | Diagnostic: when Mondial Relay was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the appropriate delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. Mondial Relay reports these:

| Status | Meaning |
|---|---|
| `registered` | Being prepared by the sender (tracking hubs) |
| `in_transit` | On its way, including the days of sorting before it reaches the pickup point |
| `at_pickup_point` | Ready to collect at the pickup point or locker |
| `delivered` | Delivered or collected |
| `problem` | Mondial Relay flags a problem with the shipment (account hubs) |
| `unknown` | A progress value Mondial Relay has no consumer meaning for, one not seen before, or a tracking code not found yet |

The carrier's own value is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Mondial Relay device):

| Event | When |
|---|---|
| `mondial_relay_parcel_registered` | A new incoming parcel appears in the active list |
| `mondial_relay_parcel_status_changed` | An incoming parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `mondial_relay_parcel_delivered` | An incoming parcel is delivered |
| `mondial_relay_parcel_delivery_time_changed` | The expected delivery window changes |
| `mondial_relay_outgoing_parcel_status_changed` | An outgoing parcel's canonical status changes (account hubs) |
| `mondial_relay_outgoing_parcel_delivered` | An outgoing parcel is delivered (account hubs) |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/).

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.mondial_relay: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Mondial Relay reported a progress value this integration does not map. A warning in the log has a link to report it; the carrier's own value is visible as `raw_status` on the parcel sensor.
- **A tracked code stays `unknown`** — Mondial Relay found no parcel for that code and the hub's postcode. Check the code, and that the postcode is the one on the label, written the same way. A parcel can also take a while to appear after the label is made.
- **"Mondial Relay's public tracking refused this integration's request" in the log** — your codes and postcode are not the problem, and the last known state is kept. Check for an integration update, and if none is available, [open an issue](https://github.com/ha-parcel-integrations/ha-mondial-relay/issues/new).
- **"Mondial Relay rejected this integration's request" in the log** — this is not a problem with your account, and signing in again will not fix it. Check for an integration update, and if none is available, [open an issue](https://github.com/ha-parcel-integrations/ha-mondial-relay/issues/new).
- **"Mondial Relay rejected that sign-in"** — the address a sign-in produces can only be used once and expires within minutes. Open the sign-in link again, sign in again, and paste the fresh address straight away.
- **"Mondial Relay's account service refused the session it had just issued"** or **"would not release its parcel list"** — the sign-in itself worked, so pasting a different address will not help. Almost always an account that has never signed in on Mondial Relay's own site or app: do that once (see [Configuration](#configuration)), then repeat the sign-in here. If it persists, [open an issue](https://github.com/ha-parcel-integrations/ha-mondial-relay/issues/new) with the warning logged under `custom_components.mondial_relay`.
- **"This account's phone number has not been confirmed"** — Mondial Relay refused this account's parcel list, and an unconfirmed phone number is the likeliest reason. Sign in to the Mondial Relay app or website with this account, confirm your phone number, then repeat the sign-in here. An unconfirmed number on its own never blocks setup.
- **A reauth prompt appears** — your Mondial Relay sign-in has expired or was revoked; repeat the browser sign-in step from [Configuration](#configuration). It reuses the country the entry was set up with.
- **The sign-in page asks for a Polish phone number** — the country was set up as a market whose sign-in page you do not have an account on. Remove the entry and add it again with the right country.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Mondial Relay, Home Assistant, or any other third party referenced in this project. Please don't contact Mondial Relay for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

An account hub signs in with your own Mondial Relay / InPost Group account through the same public sign-in page the official app uses. A tracking hub uses Mondial Relay's public tracking, with the tracking code and postcode you enter. Both may rely on public, unofficial, or undocumented carrier interfaces. These may change or be withdrawn without notice and may be subject to Mondial Relay's terms. Data is sent only to Mondial Relay's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
