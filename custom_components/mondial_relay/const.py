"""Constants for the Mondial Relay parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "mondial_relay"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Empty on purpose: the carrier's integer status vocabulary is unconfirmed, so
# every optional field this integration could someday populate — pickup point,
# ETA, weight/dimensions, a tracking URL, history — is still null on every
# parcel. Add an entry here only once a normalizer field is proven non-null
# against a real, consented account fixture.
CAPABILITIES: frozenset[str] = frozenset()

# InPost Group public OAuth/PKCE client the official app authenticates with.
MR_OAUTH_AUTHORIZE_URL = "https://account.inpost-group.com/oauth2/authorize"
MR_OAUTH_TOKEN_URL = "https://account.inpost-group.com/oauth2/token"
MR_OAUTH_CLIENT_ID = "mondialrelay-mobile"
MR_OAUTH_REDIRECT_URI = "https://account.inpost-group.com/callback"
MR_OAUTH_SCOPE = "openid"
# The sign-in page is brand- and market-scoped, and both have to be asked for.
# Without ``brand`` it serves the generic InPost sign-up whose phone step is
# locked to +48 and offers no other dial code, so a French, Belgian, Dutch,
# Spanish or Portuguese account cannot be created or signed in to at all.
MR_OAUTH_BRAND = "mr"

# The mobile account backend. Each request additionally carries a derived
# request-signing header (see signing.py) alongside the bearer token; a
# request missing or carrying a stale one is rejected before it reaches the
# account data at all.
MR_BFF_BASE_URL = "https://mobile-app-bff.mondialrelay.app/api"
MR_LIST_RECEIVED_PATH = "parcels-list-received"
MR_LIST_SHIPPED_PATH = "parcels-list-shipped"
# The account's own record. Setup asks for it before any parcel list, because
# it is the only call that separates a token the backend refuses outright from
# a token it accepts while refusing the parcel feed.
MR_USER_INFO_PATH = "user-infos"
MR_PAGE_SIZE = 20
MR_ACCEPT_LANGUAGE = "en"
MR_ORIGIN_APP = "MR"
# The backend's Cloudflare rejects Home Assistant's default aiohttp/Python
# User-Agent with a 403 before any token or signature check.
USER_AGENT = "okhttp/4.12.0"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_SUBJECT = "account_subject"
# ``userType`` from user-infos, e.g. a business ("PRO") rather than a private
# account. Kept only so a bug report says which kind of account it came from;
# nothing branches on it, and an unknown value is stored as-is.
CONF_ACCOUNT_TYPE = "account_type"
CONF_DEVICE_UID = "device_uid"
# The market the account is registered in. It steers the sign-in page only —
# the account backend is the same for every market — and entries from before
# the choice existed have no value and are treated as French.
CONF_MARKET = "market"
CONF_COUNTRY = "country"
# Live-confirmed against the sign-in page (2026-10-02): these five are
# accepted, anything else is rejected with HTTP 400 before the user ever sees
# a login form, so never widen this by guessing.
ACCOUNT_MARKETS = ("FR", "BE", "NL", "ES", "PT")
DEFAULT_ACCOUNT_MARKET = "FR"

# Where a user signs in to provision the account before setup can use it
# (see CLAUDE.md's note on the first sign-in). Live-checked 2026-10-03: the
# Spanish and Portuguese markets now serve InPost-branded sites, so a
# mondialrelay.es/.pt link would only bounce the user through a redirect.
ACCOUNT_WEBSITES = {
    "FR": "https://www.mondialrelay.fr",
    "BE": "https://www.mondialrelay.be",
    "NL": "https://www.mondialrelay.nl",
    "ES": "https://www.inpost.es",
    "PT": "https://www.inpost.pt",
}

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Kept as an option for forward compatibility, but it is currently a
# no-op: the list endpoints this integration polls carry no per-event
# timeline, only the current step, so normalize_parcel() always returns
# history=None regardless of this setting.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active parcel
# is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from (or has
# no planned_from at all); mid = anything else still in flight. The
# account-based coordinator never fully stops — the mid-tier poll is also how
# a new shipment gets discovered.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Where users report a status/event we do not map yet, or an authentication
# oddity worth a human look. The ``?template=`` parameter matters: without it
# the link opens a blank form, missing the version and log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-mondial-relay/issues/new"
    "?template=unrecognised_status.yml"
)
