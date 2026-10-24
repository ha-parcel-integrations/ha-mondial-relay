# Working in this repository

Home Assistant custom integration for **Mondial Relay** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/mondial-relay/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **The optional pickup-point summaries are a pair.** A carrier that can tell
  a parcel is destined for a pickup point exposes
  `en_route_to_pickup_point` for `pickup is true` before it arrives; one that
  can reach `ParcelStatus.AT_PICKUP_POINT` also exposes `awaiting_pickup`.
  See *Parcel contract* in `CONVENTIONS.md`. Say "pickup point", not
  "ServicePoint"/"parcel shop"/"locker", for the generic concept; the
  example carrier demonstrates both canonical sensors.

## Carrier-specific notes

**Two sources in one domain, told apart by `entry.data["source"]`.**
`account` signs in to a Mondial Relay account (everything in `account/`);
`tracking` follows codes by code + postcode (everything in `tracking/`).
Every read goes through `source.is_tracking_entry`, which defaults to
**account**: entries from before tracking hubs existed have no `source` key
and are all account hubs. This is the inverse of `ha-bpost`, whose original
source was tracking — don't copy its default. The config flow's first step
is a menu (`account` / `tracking`); the account branch is the country step,
sign-in and reauth exactly as before. `api.py`, `coordinator.py` and
`parcels.py` at the package root are re-export shims for the account source
(`tests/test_compat_imports.py` keeps them honest); nothing in the repo
imports them. What both sources share lives at the root: `polling.py`
(schedule helpers, 429 backoff), `events.py` (the bus-event contract) and
`parcel_lists.py` (sort, retention, `parse_iso`). Options branch on source
too, and the two reload models must not mix: an account hub reloads on
submit with no update listener, a tracking hub registers the update
listener and refreshes live.

**A tracking hub is one postcode.** Asked once at setup, with no network
call; every parcel in the hub then needs only its code. `unique_id` is
`tracking:<postcode>` (the prefix keeps it apart from an account's `sub`),
the title `Mondial Relay (<postcode>)` and the device name the same. Two
addresses mean two hubs. The postcode is free-form on purpose: stripped of
outer whitespace, required non-empty, and otherwise sent verbatim — no
pattern, no case or inner-space rewrite. A notation that does not match just
comes back "not found" per parcel, the same reasoning as the no-regex rule
for tracking codes below.

**The lookup is always code + postcode, with an empty brand.** A shipment
number is only unique per merchant brand, never globally: the same number
with a guessed brand can return a stranger's parcel, while code + the
recipient's postcode returns theirs. So there is no brand input, no default
and no guess (a single marketplace already uses more than one brand), and
`brand`/`codePays` are always sent empty. The code alone is refused.

**Transport: Mondial Relay's public tracking, one session per cycle.** A
`GET` of the tracking page sets cookies and carries a two-part `A:B` value on
the `id="tracking"` element; every lookup in that cycle sends it back with
the same cookies. Exactly one narrow regex reads that one attribute — nothing
else is taken from the HTML — and a page without it fails the cycle, never a
fallback. The session is the cycle's own (`aiohttp.ClientSession` on HA's
connector with its own cookie jar), closed at the end, so these cookies never
reach another integration. Plain `aiohttp` with a browser User-Agent is
enough: do not add a fingerprinting library or any other requirement. If
that stops working it is an outage to report, not a dependency to add. The
host is fixed at `mondialrelay.be`: `.be` and `.nl` return the same data, the
lookup ignores the country, and `.fr` refuses scripted clients with a `403`.
Lookups run one after another, never in parallel.

**Not found and refused are different paths — never merge them.** A `200`
JSON object without `Expedition` (a `status` list) is "not found" for that
parcel alone: both known variants carry `state: "warn"` and differ only in
translated text, so neither `state` nor the text is ever branched on. A
`403`, a `200` that is not JSON, or a page without its value is a refusal
(`MondialRelayTrackingRefusedError`): the cycle stops with `UpdateFailed`,
last-good data stays, one WARNING per entry until a cycle succeeds again,
and no retries within the cycle. A refusal after the page was just fetched
cannot be a stale page value, so it is never treated as one. A `429` backs
off as everywhere else. Any other failure of one lookup is that parcel's
problem only.

**Tracking status is `SuiviParEtapes`' highest step that carries an
`Evenement`, mapped by its integer key** (`tracking/parcels._STEP_MAP`):
`1` → `registered`, `2` → `in_transit`, `3` → `in_transit` (it spans days of
hub sorting, so it is not out for delivery), `4` → `at_pickup_point` for
`DeliveryMode` `24R` and `in_transit` for any other mode, `5` → `delivered`.
`raw_status` is the step as a string. An unknown highest step falls back to
the highest known one; no step at all is `unknown`. Every reached step that
does not map warns once, also one below a known step, because the history
skips it too and would otherwise hide it. Only
`24R` has been seen on a real parcel, so any other mode warns once and sets
`pickup: false`. No problem or returning mapping exists yet, and
`EstRetourAutorise` is a permission flag, not "returning". Never map by
`Libelle` or any event text.

**History statuses come from the steps, not the event text.** Events carry no
code of their own, so each takes the status of the latest `SuiviParEtapes`
step reached by its moment (`tracking/parcels.step_marks`): a step's own event
and everything after it until the next step share that step's status, with
the same `24R` rule for step 4. An event before any step stays `None`.

**`pickup_point` comes only from the event dated as step 5 or step 4.**
`DetailPointRelais` sits on individual events and names *that event's*
location: on the drop-off event it is the **sender's** drop-off point, on the
delivered event the collection point, and the "available" event carried none
on the one real parcel seen. So the name (`Adresse.Libelle`) is taken from the
event whose `Date` equals step 5's (then step 4's) `Evenement.Date`, only for
a `24R` parcel, never by position. In practice it fills once the parcel is
collected. It is redacted in diagnostics. `sender` is the raw `Enseigne`
merchant code (e.g. `V1VINTNL`): no name is behind it, and it is shown as is
on purpose.
`EstimatedDeliveryDate` is not a live estimate: on the one real parcel that
had it, it was the drop-off moment plus exactly eight days, written in UTC
without an offset. It is still the latest expected day, so it becomes a whole
local day for `planned_from`/`planned_to` (converted from UTC first) until
the parcel is delivered, then both are `None`. Year 1 is "no estimate yet".

**Timestamps are naive local time with a fraction of any width**, read as
`Europe/Paris` (France, Belgium, the Netherlands and Spain share it).
Portugal is an hour behind and is accepted as such — do not branch on
country for it.

**Every tracking parcel gets a `url`, built from the response, never the
postcode.** `?numeroExpedition=<Numero>&codeMarque=<Enseigne[:2]>` opens the
same parcel for anyone the link is shared with and carries no personal data.
The page follows Home Assistant's own country and language
(`tracking_page`): `NL` → `.nl`, `FR` → `.fr`, `BE` and every other country →
`.be` in French when HA's language is French, else in Dutch. Spain and
Portugal land there on purpose: those markets no longer have a Mondial
Relay-branded site. There is no country setting anywhere. Account parcels
keep `url: None`: the account record's `markAlphaCode` may be the same
two-character brand, but that is unconfirmed.

**Tracking hubs follow incoming parcels only — a decision, not a gap.** This
is a deliberate exception to "add outgoing whenever a consumer can send": a
sender has neither the recipient's postcode nor a safe lookup by brand (see
above), so a tracking hub has no outgoing list, no `direction` on
`track_parcel`, and its coordinator's `outgoing`/`delivered_outgoing` are
always empty. It also creates no outgoing summary sensors and offers no
outgoing device triggers. Parcels the user sent keep coming from an account
hub. The same parcel tracked in both kinds of hub shows up twice.

**Account model: OAuth/PKCE browser-paste, not credentials-in-HA.** Generated
with `--auth credentials`, then the password-login assumption was replaced
entirely — `config_flow.py`/`oauth.py` build the InPost Group authorization
URL, PKCE verifier/state and nonce themselves; the user opens it, signs in,
and pastes back the resulting `https://account.inpost-group.com/callback`
URL (which the flow validates for host/path/state before ever exchanging the
code). Modelled on `ha-dhl`'s DE flow — same shape, different provider. The
stored credential is a refresh token (`CONF_REFRESH_TOKEN`), never a
password. `pop_refresh_token_changed()` must be drained after every poll
(`__init__.py`'s coordinator listener) or a silently rotated refresh token is
lost on restart.

**The sign-in URL's `brand`/`lang`/`supported_markets` are load-bearing.**
Drop any of them and `account.inpost-group.com` serves the *generic InPost*
sign-up (`data-brand="inpost"`, Polish copy) instead of Mondial Relay's — and
that page's phone step is locked to +48 with no other dial code on offer, so
no French, Belgian, Dutch, Spanish or Portuguese account can be created or
signed in to at all (#4). `brand=mr` is the switch: live-confirmed: `supported_markets=BE&lang=nl-BE` *without* `brand` still renders
the Polish page. The market therefore has to be asked **before** the link is
built — hence `CONF_MARKET` in `entry.data` and the country step ahead of the
paste step, reused as-is on reauth. `ACCOUNT_MARKETS` is the live-confirmed
set (`FR`/`BE`/`NL`/`ES`/`PT`); anything else is an HTTP 400 on the authorize
call, so never widen it by guessing. `lang` carries a region because the
page's own script falls back to the locale's region to
preselect a dial code — which is also why `es-ES`/`pt-PT` are sent even
though that page has no Spanish or Portuguese translation and renders French.

**Two different failure paths — never merge them.** A 401 (rejected *user*
token) is `MondialRelayAuthError` → HA reauth. A 403 with a valid user token
is `MondialRelaySigningRejectedError` (headers from `signing.py` rejected) →
abort the poll, keep last-good data, log one WARNING, never reauth or fall
back; only an integration update fixes it.

**An account must sign in on Mondial Relay's own site or app once before
this integration can use it** — confirmed by the reporter in #4, who had
completed the OAuth sign-in successfully and still got nothing until they
logged in at `mondialrelay.be` first, after which the same build worked. This
provisioning is server-side, triggered by that first sign-in; there is no
activation *call* this integration could make. It cannot be done from here, so it belongs in the instructions, and
`ACCOUNT_WEBSITES` carries the per-market URL the `sign_in` step shows. That
map is live-checked: `ES`/`PT` serve InPost-branded sites, so
`mondialrelay.es`/`.pt` would only bounce through a redirect. It is also why
`account_rejected` and `parcels_unavailable` name this remedy first instead
of sending the user straight to the issue tracker.

**Setup has four refusals, and they must stay separate.** Merging any of
them back together is what made #4 undiagnosable, so each logs one WARNING
naming the side that refused:

| Where it failed | Error | What it means |
|---|---|---|
| Code exchange, IdP's own `400`/`401` (on `MondialRelayOAuthError.error_code`) | `invalid_auth` | Almost always a reused or expired authorization code; a fresh sign-in fixes it. |
| `user-infos` `401` | `account_rejected` | The backend refused the session it had just issued. The sign-in worked, so no other pasted URL helps. |
| `parcels-list-received` `401` **and** `user-infos` said `phone.valid is False` | `phone_not_confirmed` | The likeliest explanation for that refusal, and one the user can act on. |
| `parcels-list-received` `401` with a confirmed number | `parcels_unavailable` | Identity is fine, the parcel feed alone refused. Nothing the user can fix. |

**`phone.valid` explains a refusal; it never gates setup.** That the backend
*requires* a confirmed number is an inference from the app's
`PhoneConfirmation_*` screens, not something observed on the wire — so an
unconfirmed number on an account whose parcel list loads fine must still set
up successfully. Only an explicit `false` counts as unconfirmed; a missing or
reshaped `phone` object warns once (`api.has_confirmed_phone`) and passes.

**The account's `stepSection` is the app's own step-section enum, mapped by
its integer value** (`account/parcels._STEP_SECTION_MAP`): `1` → `at_pickup_point`, `2` →
`in_transit`, `3` → `delivered`. `0` and `4` have no consumer meaning and stay
`unknown` with the one-shot warning, like any value outside the map. `3` is
confirmed on two real accounts (#8); `1`/`2` rest on the app alone, so treat a
report contradicting them as data, not noise. `raw_status` stays
`str(stepSection)`. `hasProblem: true` turns any non-delivered parcel into
`problem`. `delivered_at` is the delivered parcel's `tracingDate` and is
redacted in diagnostics like the raw field. Never map by
`stepHint`/`stepPictoName`/a tracing code/translated text.

**`CAPABILITIES_BY_VARIANT["Account"]` is deliberately empty**
(`CAPABILITIES`/`PENDING_CAPABILITIES` alias the account variant). The
`Tracking` variant claims `delivery_window`, `pickup_point`, `url` and
`history`, which the tracking response carries. On the account, every optional field —
`planned_from`/`planned_to`, `pickup_point`, `url`, `weight`, `dimensions`,
`history` — is `None` until a consented account fixture (covering both list
endpoints, plus `parcels-detail`) confirms it. `pickup` stays `False` even
for a `locker: true` shipment; that field's meaning for the *recipient* is
unconfirmed.

**`raw` is the full list item** (`{expedition, delivery}`), untrimmed.
Privacy lives only in `diagnostics.py`'s redaction set — when the fixture
reveals new identifying keys, add them there, never trim `raw`.

**Inbox split: two list endpoints, deduplicated independently, never
reconciled against each other.** `parcels-list-received` → incoming,
`parcels-list-shipped` → outgoing. `parcels.dedupe_by_shipment_uid` drops a
repeated `expedition.shipmentUid` *within* one list, keeping the first; a UID
appearing in *both* lists is left exactly as the server returned it (counted
in both), with one value-free WARNING per session
(`parcels.warn_inbox_overlap_once`). Do not try to pick a single "true"
direction for an overlapping shipment — the server's own split is the source
of truth, not a heuristic here.

**Four of the BFF's seven routes are deliberately not called.** The app's own
Retrofit interface has `parcels-list-received`, `parcels-list-shipped`,
`parcels-detail`, `parcels-search`, `parcels-list-not-migrated`,
`parcel-detail-not-migrated` and a `POST easy-access-zone`. We call the two
lists plus `user-infos`. `parcels-detail`'s `parcelType` is confirmed as
`"shipped"`/`"received"` (from the app), but its response envelope is not, and
`parcels-search` is still unprobed — both out of scope until a
consented `parcels-detail` fixture exists. The **`*-not-migrated` pair is a decision, not
a gap**: that list only holds a shipment the user created minutes ago (the
app's own copy is *"Le colis est en cours de création. Veuillez patienter
quelques minutes."*), so on a 15–45 minute poll it would buy a sensor that
appears and vanishes again, at the cost of an extra call every cycle. Revisit
only if a user actually reports a just-created parcel missing.

**The InPost Group sign-in is the app's only one since app 1.28.0.** Earlier
app versions could be switched server-side back to an older login that this
integration does not implement; that switch is gone from 1.28.0. If every
account suddenly fails to authenticate at once, look at a change on
Mondial Relay's side first, not a local cause, but no longer expect a silent
fallback to the old login.

**Entry title is the literal string `"Mondial Relay"` for every account**
(tracking hubs carry their postcode instead, see above) —
not per-account, unlike most other multi-account carriers in this suite.
Two configured accounts are told apart by the **device** name instead
(`device.py` suffixes a non-reversible slice of `CONF_ACCOUNT_SUBJECT`), not
the config entry title. Don't "fix" this to look like DHL's per-account
title — it is the setup contract this integration was built to.

**Setup validates in two steps, identity before parcels.**
`account/api.py::async_get_user_info` (`user-infos`) first, then
`async_validate_parcel_access` (`parcels-list-received`, page 0 only, never
the full paginated fetch). The order is the whole point: it is what splits
`account_rejected` from `parcels_unavailable` in the table above. `user-infos`
is the first call the official app makes after a login, so it is not an extra
burden on the backend. It is a setup-only call — the coordinator never makes
it, because nothing in a poll needs it.

**`unique_id` stays the OAuth ID token's `sub` claim**
(`oauth.decode_id_token_subject`) even though `user-infos` now gives us
`guid` and `ucPersonId` as well. Switching would mean a network-dependent
migration of every existing entry, and running two schemes side by side would
let the same account be added twice without being deduplicated — all for an
identifier that is no more stable than the one we have. The record's
`userType` *is* stored, as `CONF_ACCOUNT_TYPE`, purely so a bug report says
whether it came from a business (`PRO`) account; nothing branches on it and
diagnostics deliberately do not redact it.

## Options and reloads

For code-based carriers, the options flow starts with exactly `Parcels` and
`Settings` — or, where the carrier supports outgoing parcels, `Incoming
parcels` / `Outgoing parcels` / `Settings` (see the next section).
`Parcels` is one editable multi-code list; `Settings` is
a flat form — some carriers use one sectioned form
(`data_entry_flow.section`) instead; both are generator variants, not carrier
decisions. Changes apply without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default, and the `--auth byo-key` build) apply
  changes live: an update listener calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately (this is also the resume
  path after polling has fully suspended — see "Dynamic polling" below).
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Auth models

Three `--auth` builds, one axis: **what the config flow asks for and
validates**, not how tracking works. `none` and `byo-key` both key tracking on
codes the user types in (`Parcels`/`Settings` options, `track_parcel` /
`untrack_parcel` services, the account-less coordinator and its full-stop /
delivered-skip behaviour below) — `byo-key` only adds a required key field
validated against the carrier's **official** API at setup, the key in
`entry.data`, and a reauth flow for when the key is rotated or revoked outside
Home Assistant (`MondialRelayAuthError` from any per-parcel fetch raises
`ConfigEntryAuthFailed` for the whole poll — one credential covers every
tracked code, so a rejected key is never treated as one parcel's problem).
`credentials` is the only one that changes the *tracking* model too (an
account feed, not user-entered codes) — see the split above.

Reach for `byo-key` only when carrier-research has already established that
the carrier's *unauthenticated/consumer* surface is unusable (bot-walled,
requires a session a script can't hold) and that the *official* developer key
is reachable by a private individual — `key_access: consumer` in the research
doc's front matter, not `business`. A `business`-gated key is a wall, not a
BYO key, whatever the portal's own copy claims (see
`carrier-research/CLAUDE.md`'s "Standing rulings").

## Incoming and outgoing parcels

**Add outgoing support whenever the carrier lets a consumer send a parcel** —
a C2C shipment, a locker drop-off, a marketplace or returns label. It is not
an optional extra to bolt on later: without it a parcel the user sent counts
towards `incoming_active` and sits on their dashboard next to the ones they
are waiting for. A carrier that genuinely has no consumer-sending surface is
exempt — say so in *Carrier-specific notes* so the gap reads as a decision.

Where the direction comes from has exactly two answers, and which one applies
follows from the carrier, not from taste:

- **Account-based** — the account feed distinguishes them, so derive it and
  never ask the user. A shipment matching neither side logs a one-shot
  warning and defaults to incoming rather than disappearing from every list.
  References: `ha-ppl-cz` (one call, split on a field), `ha-dhl-nl` (a
  separate "sent" endpoint).
- **Account-less** — the payload cannot reveal it (the user's own parcel and
  a stranger's look alike, and there is no account identity to compare a
  party against), so the **user declares it per parcel**: two menu entries in
  the options flow, each the same multi-code list, plus a `direction` field on
  `track_parcel`. Store it as `CONF_DIRECTION` on the `CONF_PARCELS` dicts,
  defaulting to incoming, so entries written before the option existed need no
  migration. Re-filing a code under the other direction moves it instead of
  erroring — that is the correction path. Reference: `ha-packeta`.
  **Never infer direction from free-text event wording**: a handover sentence
  that happens to name the drop-off point is not a structured field, says
  nothing before handover, and silently ties the split to one locale.

Above that split the shape is identical either way, and is suite-wide:
`coordinator.outgoing` / `coordinator.delivered_outgoing` alongside `data` /
`delivered`; `outgoing_parcels` + `outgoing_delivered_parcels` summary
sensors (their unique_ids belong in `non_parcel_unique_ids`); per-parcel
sensors spawned for **both** directions; the
`<domain>_outgoing_parcel_status_changed` / `_outgoing_parcel_delivered`
event pair, with **no** `registered` and no delivery-time event for outgoing;
`awaiting_pickup`, `next_delivery` and the calendar staying incoming-only;
and both new lists in `diagnostics.py`. The aggregator needs no change — it
buckets on the sensor suffix and the event prefix. Also set
`directions: incoming+outgoing` for the carrier in the docs site's
`data/carriers.yml`.

## Tracking-code validation

In code-based carriers (account-based sources have no code entry),
`valid_tracking_code` in `config_flow.py` accepts every non-empty code — no
format regex. This is a suite-wide convention, not a per-carrier TODO: real
tracking-number formats vary too much across carriers, and are often not
fully confirmed even for this one, to gate on a guessed shape. A too-strict
regex risks rejecting a genuinely valid code; an actually-bad code just comes
back "not found" on the next poll, which is a far cheaper failure mode. Do
not add one back in, even once the format is confirmed.

## Dynamic polling

There is no user-facing polling interval — this is a deliberate suite-wide
choice, not a gap. `coordinator.py`'s `_hottest_tier_minutes` /
`_next_update_interval` recompute `update_interval` at the end of every
refresh. The reference carrier in `ha-carrier-template` (its `coordinator.py`)
is the canonical implementation every carrier mirrors; the design rationale (quiet window, tiers, stagger,
backoff, delivered-skip) is spelled out below.

- **Quiet window:** no polling 00:00–06:00 local time, except two daily
  anchors (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight —
  `problem`/`returning` included, deliberately not hot. Account-based carriers
  never fully stop even with nothing hot or in transit: the mid-tier poll is
  also how a new shipment gets discovered.
- **Full stop (account-less carriers only):** `update_interval = None` when
  nothing is tracked or every tracked parcel is delivered. Resumes the moment
  a parcel is added back, via the options-flow refresh above.
- **Stagger:** a small, stable per-install offset (hash of the config entry
  id) is added to every computed interval so installs don't all hit an anchor
  or tier boundary at the same second.
- **429 backoff:** a 429 anywhere in a poll raises `UpdateFailed` with
  `retry_after` — the carrier's own `Retry-After` header if present, otherwise
  an exponential backoff tracked per-coordinator. `api.py`'s
  `…ApiError.status_code` / `.retry_after` carry this from the HTTP layer.
- **Delivered codes are skipped from the fetch (account-less carriers only):**
  once a tracking code's payload comes back `delivered`, `coordinator.py`
  excludes it from the next cycle's fetch — its payload can never change
  again. `self._delivered_codes` (keyed on the tracking code, not the barcode)
  is rebuilt from each cycle's results and intersected with the tracked set on
  untrack. The code stays in the options list, keeps its sensor and its
  cached payload, and still shows under the retention window — it just costs
  no more requests. `coordinator.delivered_codes` surfaces the count in
  diagnostics. Account-based carriers have nothing to skip here — one account
  call already returns everything, so they either have no `delivered_codes` or
  it is always empty.

A carrier that genuinely throttles or soft-bans traffic harder than the 429
backoff handles is a documented, local divergence from this in that one
repo's own `CLAUDE.md` — not a generator flag.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types; `account/` and `tracking/` packages instead when a repo has two sources) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (the status map, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation; key/credential validation on `--auth byo-key`/`credentials`) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `device.py` (shared device-info helper) | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.mondial_relay
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
