"""Singapore Postal District definitions and resolution helpers."""

from collections.abc import Iterable
from typing import Any

POSTAL_DISTRICTS: tuple[dict[str, Any], ...] = (
    {
        "district": 1,
        "name": "Raffles Place, Cecil, Marina, People's Park",
        "postal_sectors": ["01", "02", "03", "04", "05", "06"],
    },
    {
        "district": 2,
        "name": "Anson, Tanjong Pagar",
        "postal_sectors": ["07", "08"],
    },
    {
        "district": 3,
        "name": "Queenstown, Tiong Bahru",
        "postal_sectors": ["14", "15", "16"],
    },
    {
        "district": 4,
        "name": "Telok Blangah, Harbourfront",
        "postal_sectors": ["09", "10"],
    },
    {
        "district": 5,
        "name": "Pasir Panjang, Hong Leong Garden, Clementi New Town",
        "postal_sectors": ["11", "12", "13"],
    },
    {
        "district": 6,
        "name": "High Street, Beach Road (part)",
        "postal_sectors": ["17"],
    },
    {
        "district": 7,
        "name": "Middle Road, Golden Mile",
        "postal_sectors": ["18", "19"],
    },
    {
        "district": 8,
        "name": "Little India",
        "postal_sectors": ["20", "21"],
    },
    {
        "district": 9,
        "name": "Orchard, Cairnhill, River Valley",
        "postal_sectors": ["22", "23"],
    },
    {
        "district": 10,
        "name": "Ardmore, Bukit Timah, Holland Road, Tanglin",
        "postal_sectors": ["24", "25", "26", "27"],
    },
    {
        "district": 11,
        "name": "Watten Estate, Novena, Thomson",
        "postal_sectors": ["28", "29", "30"],
    },
    {
        "district": 12,
        "name": "Balestier, Toa Payoh, Serangoon",
        "postal_sectors": ["31", "32", "33"],
    },
    {
        "district": 13,
        "name": "Macpherson, Braddell",
        "postal_sectors": ["34", "35", "36", "37"],
    },
    {
        "district": 14,
        "name": "Geylang, Eunos, Paya Lebar",
        "postal_sectors": ["38", "39", "40", "41"],
    },
    {
        "district": 15,
        "name": "Katong, Joo Chiat, Amber Road",
        "postal_sectors": ["42", "43", "44", "45"],
    },
    {
        "district": 16,
        "name": "Bedok, Upper East Coast, Eastwood, Kew Drive",
        "postal_sectors": ["46", "47", "48"],
    },
    {
        "district": 17,
        "name": "Loyang, Changi",
        "postal_sectors": ["49", "50", "81"],
    },
    {
        "district": 18,
        "name": "Tampines, Pasir Ris",
        "postal_sectors": ["51", "52"],
    },
    {
        "district": 19,
        "name": "Serangoon Garden, Hougang, Punggol",
        "postal_sectors": ["53", "54", "55", "82"],
    },
    {
        "district": 20,
        "name": "Bishan, Ang Mo Kio",
        "postal_sectors": ["56", "57"],
    },
    {
        "district": 21,
        "name": "Upper Bukit Timah, Clementi Park, Ulu Pandan",
        "postal_sectors": ["58", "59"],
    },
    {
        "district": 22,
        "name": "Jurong",
        "postal_sectors": ["60", "61", "62", "63", "64"],
    },
    {
        "district": 23,
        "name": "Hillview, Dairy Farm, Bukit Panjang, Choa Chu Kang",
        "postal_sectors": ["65", "66", "67", "68"],
    },
    {
        "district": 24,
        "name": "Lim Chu Kang, Tengah",
        "postal_sectors": ["69", "70", "71"],
    },
    {
        "district": 25,
        "name": "Kranji, Woodgrove",
        "postal_sectors": ["72", "73"],
    },
    {
        "district": 26,
        "name": "Upper Thomson, Springleaf",
        "postal_sectors": ["77", "78"],
    },
    {
        "district": 27,
        "name": "Yishun, Sembawang",
        "postal_sectors": ["75", "76"],
    },
    {
        "district": 28,
        "name": "Seletar",
        "postal_sectors": ["79", "80"],
    },
)

SECTOR_TO_DISTRICT: dict[str, tuple[int, str]] = {
    sector: (d["district"], d["name"])
    for d in POSTAL_DISTRICTS
    for sector in d["postal_sectors"]
}

DISTRICT_BY_NUMBER: dict[int, dict[str, Any]] = {
    d["district"]: d for d in POSTAL_DISTRICTS
}


def get_all_postal_districts() -> list[dict[str, Any]]:
    """Return all 28 canonical Postal Districts ordered ascending by district number."""
    return list(POSTAL_DISTRICTS)


def get_district_by_postal_code(postal: str | None) -> tuple[int | None, str | None]:
    """Resolve district number and name from a 6-digit postal code string.

    Returns (None, None) if postal is None, shorter than 2 digits,
    non-numeric prefix, or unmapped sector (e.g. 74).
    """
    if postal is None:
        return None, None

    cleaned = postal.strip()
    if len(cleaned) < 2:
        return None, None

    sector = cleaned[:2]
    if not sector.isdigit():
        return None, None

    match = SECTOR_TO_DISTRICT.get(sector)
    if match is None:
        return None, None

    return match[0], match[1]


def get_postal_sectors_for_districts(districts: Iterable[int]) -> list[str]:
    """Resolve union of unique two-digit postal sector strings for given district numbers."""
    sectors: set[str] = set()
    for district in districts:
        district_data = DISTRICT_BY_NUMBER.get(district)
        if district_data:
            sectors.update(district_data["postal_sectors"])
    return sorted(sectors)
