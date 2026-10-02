"""US state abbreviations, canonical names, and normalization."""
from __future__ import annotations

US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_CODE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}
_CODE_BY_NAME["washington dc"] = "DC"
_CODE_BY_NAME["washington, d.c."] = "DC"


def normalize_us_state(value: str | None) -> str | None:
    """Return the two-letter code for a US state abbreviation or name, else None.

    Case and surrounding whitespace are ignored: ``"wy"``, ``"WY"`` and
    ``" wyoming "`` all return ``"WY"``.
    """
    if not value:
        return None
    cleaned = " ".join(str(value).split())
    code = cleaned.upper()
    if code in US_STATES:
        return code
    return _CODE_BY_NAME.get(cleaned.lower())
