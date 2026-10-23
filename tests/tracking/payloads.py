"""Synthetic Mondial Relay tracking payloads shared by the tracking tests.

Shapes follow real responses; every value is made up.
"""

from __future__ import annotations

import copy
from typing import Any

TRACKING_CODE = "12345678"
POSTAL_CODE = "9999"
TOKEN = "AbCdEf123_-xyz:GhIjKl456_-uvw"

TRACKING_PAGE = (
    '<html><body><div class="app-wrapper"><div id="tracking" shopId="70" '
    f'token="{TOKEN}"></div></div></body></html>'
)
TRACKING_PAGE_WITHOUT_TOKEN = (
    '<html><body><div class="app-wrapper"><div id="tracking" shopId="70">'
    "</div></div></body></html>"
)

NOT_FOUND = {
    "status": [
        {"state": "warn", "message": "Er zijn geen pakketten voor deze zoekcriteria"}
    ]
}
INVALID_REQUEST = {
    "status": [{"state": "warn", "message": "Fout bij het valideren van het verzoek"}]
}

_DROP_OFF_POINT = {
    "Numero": 11111,
    "Horaires": [],
    "Services": [],
    "Adresse": {
        "Libelle": "TEST SHOP",
        "LibelleComplement": "",
        "AdresseLigne1": "Teststraat 1",
        "AdresseLigne2": "",
        "CodePostal": "0000",
        "Ville": "Testdorp",
        "CodePays": "NL",
    },
}
_COLLECTION_POINT = {
    "Numero": 22222,
    "Horaires": [],
    "Services": [],
    "Adresse": {
        "Libelle": "OTHER TEST SHOP",
        "LibelleComplement": "",
        "AdresseLigne1": "Testlaan 2",
        "AdresseLigne2": "",
        "CodePostal": "0001",
        "Ville": "Teststad",
        "CodePays": "BE",
    },
}

_AVAILABLE: dict[str, Any] = {
    "Numero": 12345678,
    "Enseigne": "ZZTESTNL",
    "DestinataireCodePostal": POSTAL_CODE,
    "CodeAgenceLivraison": 1234,
    "Evenements": [
        {
            "Date": "2030-01-01T10:00:00.123",
            "Libelle": "Pakje in voorbereiding bij de verzender ",
        },
        {
            "Date": "2030-01-02T15:00:00.45",
            "Libelle": "Pakje opgehaald in een Afhaalpunt ",
            "DetailPointRelais": _DROP_OFF_POINT,
        },
        {
            "Date": "2030-01-03T02:00:00.001",
            "Libelle": "in ontvangstname in het sorteercentrum",
        },
        {
            "Date": "2030-01-04T08:00:00",
            "Libelle": "Pakje onderweg naar het afhalingspunt ",
        },
        {
            "Date": "2030-01-04T10:00:00.868",
            "Libelle": "Nog 5 dagen voor de afhaling van het pakje in het Afhaalpunt ",
        },
    ],
    "EvenementsNonClientChargeur": [
        {"Date": "2030-01-02T15:00:01.01", "Code": "EML", "SousCode": "DPR"},
        {"Date": "2030-01-03T01:00:00.001", "Code": "ARR", "SousCode": "CAM"},
    ],
    "SuiviParEtapes": {
        "5": {"Numero": 5, "Libelle": "Pakket is afgeleverd"},
        "4": {
            "Evenement": {"Date": "2030-01-04T10:00:00.868"},
            "Numero": 4,
            "Libelle": "Pakket beschikbaar in het afhaalpunt",
        },
        "3": {
            "Evenement": {"Date": "2030-01-03T02:00:00.001"},
            "Numero": 3,
            "Libelle": "Pakket in distributie",
        },
        "2": {
            "Evenement": {"Date": "2030-01-02T15:00:00.45"},
            "Numero": 2,
            "Libelle": "Pakket geleverd  bij Mondial Relay",
        },
        "1": {
            "Evenement": {"Date": "2030-01-01T10:00:00.123"},
            "Numero": 1,
            "Libelle": "Pakket in voorbereiding",
        },
    },
    "SuiviContextuel": "Uw pakket ligt klaar in het afhaalpunt.",
    "NaturesPointRelaisCompatibles": ["E", "F", "1", "D", "C"],
    "EstRetourAutorise": False,
    "EstimatedDeliveryDate": "2030-01-05T13:52:54",
    "CollectCountryParcel": "NL",
    "DeliveryCountryParcel": "BE",
    "DeliveryMode": "24R",
}


def available() -> dict[str, Any]:
    """A parcel ready at the pickup point (step 4)."""
    return copy.deepcopy(_AVAILABLE)


def delivered() -> dict[str, Any]:
    """The same parcel collected at the pickup point (step 5)."""
    expedition = available()
    expedition["Evenements"].append(
        {
            "Date": "2030-01-06T14:43:23.783",
            "Libelle": "afgeleverd pakje",
            "DetailPointRelais": copy.deepcopy(_COLLECTION_POINT),
        }
    )
    expedition["SuiviParEtapes"]["5"]["Evenement"] = {"Date": "2030-01-06T14:43:23.783"}
    return expedition


def in_distribution() -> dict[str, Any]:
    """The parcel in hub sorting (step 3), without an estimate yet."""
    expedition = available()
    del expedition["SuiviParEtapes"]["4"]["Evenement"]
    expedition["Evenements"] = expedition["Evenements"][:3]
    expedition["EstimatedDeliveryDate"] = "0001-01-01T00:00:00"
    return expedition


def at_step(step: int) -> dict[str, Any]:
    """The parcel with steps 1..``step`` reached and nothing beyond."""
    expedition = available()
    for key, entry in expedition["SuiviParEtapes"].items():
        if int(key) <= step:
            entry["Evenement"] = {"Date": f"2030-01-0{key}T10:00:00"}
        else:
            entry.pop("Evenement", None)
    return expedition


def found(expedition: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap an ``Expedition`` the way the lookup returns it."""
    return {"Expedition": expedition or available(), "status": []}
