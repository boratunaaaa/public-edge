"""SIC code -> broad sector (used for sector-equal weighting; ~12 buckets like GICS sectors)."""
from __future__ import annotations


def sector_for_sic(sic: str | int | None) -> str | None:
    if sic in (None, ""):
        return None
    try:
        c = int(str(sic).strip())
    except ValueError:
        return None
    g2, g3 = c // 100, c // 10
    if c == 6770:
        return "SHELL_SPAC"
    if 2833 <= c <= 2836 or c == 8731:
        return "PHARMA_BIOTECH"
    if g3 in (384, 385) or g2 == 80 or c == 8071:
        return "HEALTH_CARE"
    if g3 in (357, 366, 367) or c in (3825, 3826, 3827, 3829) or g3 == 737:
        return "TECHNOLOGY"
    if g2 == 48:
        return "COMMUNICATIONS"
    if g2 == 49:
        return "UTILITIES"
    if 10 <= g2 <= 14 or g2 == 29:
        return "ENERGY_MINING"
    if c == 6798 or g2 == 65:
        return "REAL_ESTATE"
    if 60 <= g2 <= 67:
        return "FINANCIALS"
    if g2 in (24, 26, 28, 30, 32, 33):
        return "MATERIALS"
    if 15 <= g2 <= 17 or g2 in (34, 35, 36, 37, 38) or 40 <= g2 <= 47 or g2 == 50:
        return "INDUSTRIALS"
    if 20 <= g2 <= 23 or g2 in (25, 27, 31, 39, 51) or 52 <= g2 <= 59 or g2 in (70, 72, 75, 78, 79):
        return "CONSUMER"
    if 1 <= g2 <= 9:
        return "AGRICULTURE"
    if 70 <= g2 <= 89:
        return "SERVICES"
    if 91 <= g2 <= 99:
        return "OTHER"
    return "OTHER"
