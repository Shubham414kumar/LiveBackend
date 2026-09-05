"""Country emergency service numbers.

A curated static table. Static is the right call here: these numbers change
rarely, and an app that dials an emergency service must not depend on a network
round trip to a third party to know what number to dial.

The fallback matters as much as the data. Returning "112" for an unknown country
with no caveat is dangerous — 112 is a GSM standard that reaches *a* dispatcher
on most networks, but it is not the correct local number everywhere, and in some
countries it will not connect from a landline at all. Unknown countries are
therefore returned with ``verified: False`` and an explicit note, so the UI can
say so rather than presenting a guess as fact.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Reaches emergency services on most GSM networks worldwide, including from a
# locked handset with no SIM. Not a substitute for the correct local number.
UNIVERSAL_GSM_NUMBER = "112"

DEFAULT_CONTACTS: Dict[str, Optional[str]] = {
    "general": UNIVERSAL_GSM_NUMBER,
    "police": UNIVERSAL_GSM_NUMBER,
    "ambulance": UNIVERSAL_GSM_NUMBER,
    "fire": UNIVERSAL_GSM_NUMBER,
    "disaster": None,
}

EMERGENCY_CONTACTS: Dict[str, Dict[str, Any]] = {
    "IN": {
        "country": "India",
        "general": "112",
        "police": "100",
        "ambulance": "102",
        "fire": "101",
        "disaster": "108",
        "women_helpline": "1091",
        "child_helpline": "1098",
        "cyber": "1930",
    },
    "US": {
        "country": "United States",
        "general": "911",
        "police": "911",
        "ambulance": "911",
        "fire": "911",
        "disaster": "211",
    },
    "GB": {
        "country": "United Kingdom",
        "general": "999",
        "police": "999",
        "ambulance": "999",
        "fire": "999",
        "disaster": "112",
    },
    "CA": {
        "country": "Canada",
        "general": "911",
        "police": "911",
        "ambulance": "911",
        "fire": "911",
    },
    "AU": {
        "country": "Australia",
        "general": "000",
        "police": "000",
        "ambulance": "000",
        "fire": "000",
    },
    "JP": {
        "country": "Japan",
        "general": "110",
        "police": "110",
        "ambulance": "119",
        "fire": "119",
    },
    "CN": {
        "country": "China",
        "general": "110",
        "police": "110",
        "ambulance": "120",
        "fire": "119",
    },
    "DE": {
        "country": "Germany",
        "general": "112",
        "police": "110",
        "ambulance": "112",
        "fire": "112",
    },
    "FR": {"country": "France", "general": "112", "police": "17", "ambulance": "15", "fire": "18"},
    "IT": {
        "country": "Italy",
        "general": "112",
        "police": "113",
        "ambulance": "118",
        "fire": "115",
    },
    "ES": {
        "country": "Spain",
        "general": "112",
        "police": "091",
        "ambulance": "061",
        "fire": "080",
    },
    "RU": {
        "country": "Russia",
        "general": "112",
        "police": "102",
        "ambulance": "103",
        "fire": "101",
    },
    "BR": {
        "country": "Brazil",
        "general": "190",
        "police": "190",
        "ambulance": "192",
        "fire": "193",
    },
    "MX": {"country": "Mexico", "general": "911"},
    "ZA": {"country": "South Africa", "general": "112", "police": "10111", "ambulance": "10177"},
    "AE": {
        "country": "United Arab Emirates",
        "general": "999",
        "police": "999",
        "ambulance": "998",
        "fire": "997",
    },
    "SA": {
        "country": "Saudi Arabia",
        "general": "911",
        "police": "999",
        "ambulance": "997",
        "fire": "998",
    },
    "PK": {
        "country": "Pakistan",
        "general": "15",
        "police": "15",
        "ambulance": "1122",
        "fire": "16",
    },
    "BD": {"country": "Bangladesh", "general": "999"},
    "NP": {
        "country": "Nepal",
        "general": "112",
        "police": "100",
        "ambulance": "102",
        "fire": "101",
    },
    "LK": {
        "country": "Sri Lanka",
        "general": "119",
        "police": "119",
        "ambulance": "110",
        "fire": "110",
    },
    "MY": {"country": "Malaysia", "general": "999"},
    "SG": {
        "country": "Singapore",
        "general": "999",
        "police": "999",
        "ambulance": "995",
        "fire": "995",
    },
    "TH": {
        "country": "Thailand",
        "general": "191",
        "police": "191",
        "ambulance": "1669",
        "fire": "199",
    },
    "ID": {
        "country": "Indonesia",
        "general": "112",
        "police": "110",
        "ambulance": "118",
        "fire": "113",
    },
    "PH": {"country": "Philippines", "general": "911"},
    "VN": {
        "country": "Vietnam",
        "general": "113",
        "police": "113",
        "ambulance": "115",
        "fire": "114",
    },
    "KR": {
        "country": "South Korea",
        "general": "112",
        "police": "112",
        "ambulance": "119",
        "fire": "119",
    },
    "TR": {"country": "Turkey", "general": "112"},
    "EG": {
        "country": "Egypt",
        "general": "122",
        "police": "122",
        "ambulance": "123",
        "fire": "180",
    },
    "NG": {"country": "Nigeria", "general": "112"},
    "KE": {"country": "Kenya", "general": "999", "police": "999", "ambulance": "999"},
    "IL": {
        "country": "Israel",
        "general": "100",
        "police": "100",
        "ambulance": "101",
        "fire": "102",
    },
    "IR": {"country": "Iran", "general": "110", "police": "110", "ambulance": "115", "fire": "125"},
    "UA": {
        "country": "Ukraine",
        "general": "112",
        "police": "102",
        "ambulance": "103",
        "fire": "101",
    },
    "PL": {
        "country": "Poland",
        "general": "112",
        "police": "997",
        "ambulance": "999",
        "fire": "998",
    },
    "NL": {"country": "Netherlands", "general": "112"},
    "SE": {"country": "Sweden", "general": "112"},
    "NO": {
        "country": "Norway",
        "general": "112",
        "police": "112",
        "ambulance": "113",
        "fire": "110",
    },
    "FI": {"country": "Finland", "general": "112"},
    "GR": {
        "country": "Greece",
        "general": "112",
        "police": "100",
        "ambulance": "166",
        "fire": "199",
    },
    "CH": {
        "country": "Switzerland",
        "general": "112",
        "police": "117",
        "ambulance": "144",
        "fire": "118",
    },
    "AR": {
        "country": "Argentina",
        "general": "911",
        "police": "911",
        "ambulance": "107",
        "fire": "100",
    },
    "CL": {
        "country": "Chile",
        "general": "133",
        "police": "133",
        "ambulance": "131",
        "fire": "132",
    },
    "CO": {"country": "Colombia", "general": "123"},
    "PE": {"country": "Peru", "general": "105", "police": "105", "ambulance": "106", "fire": "116"},
    "NZ": {"country": "New Zealand", "general": "111"},
}


def emergency_contacts_for(iso2: Optional[str]) -> Dict[str, Any]:
    """Look up emergency numbers for an ISO-3166 alpha-2 country code.

    Always returns something dialable, but flags whether it is verified for that
    country so the client can present a guess as a guess.
    """
    code = (iso2 or "").strip().upper()
    if code and code in EMERGENCY_CONTACTS:
        entry = dict(EMERGENCY_CONTACTS[code])
        entry["iso2"] = code
        entry["verified"] = True
        return entry

    return {
        **DEFAULT_CONTACTS,
        "country": "Unknown",
        "iso2": code or None,
        "verified": False,
        "note": (
            f"No verified numbers on file for this location. {UNIVERSAL_GSM_NUMBER} "
            "reaches emergency services on most mobile networks worldwide, but "
            "confirm the correct local number where you are."
        ),
    }


def supported_countries() -> list[str]:
    return sorted(EMERGENCY_CONTACTS.keys())


__all__ = [
    "DEFAULT_CONTACTS",
    "EMERGENCY_CONTACTS",
    "UNIVERSAL_GSM_NUMBER",
    "emergency_contacts_for",
    "supported_countries",
]
