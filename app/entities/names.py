"""Company / person name normalization used for strict (exact-after-normalization) matching."""
from __future__ import annotations

import re
import unicodedata

_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "L L C", "LTD", "LIMITED", "PLC",
    "LP", "L P", "LLP", "HOLDINGS", "HOLDING", "GROUP", "THE", "SA", "NV", "AG", "SE", "N A", "NA", "DE", "DEL",
    "CL A", "CLASS A", "US", "USA",
}
_SUFFIX_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(s) for s in _SUFFIXES), key=len, reverse=True)) + r")\b")


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"/[A-Z]{2,3}/?$", " ", s)  # EDGAR state suffixes like "/DE/"
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = " ".join(s.split())
    prev = None
    while prev != s:  # strip trailing legal suffixes repeatedly ("XYZ HOLDINGS INC")
        prev = s
        s = re.sub(r"(\s|^)(" + "|".join(sorted((re.escape(x) for x in _SUFFIXES), key=len, reverse=True)) + r")$", "", s).strip()
    s = re.sub(r"^THE ", "", s)
    return s


def normalize_person(name: str | None) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"\b(HON|MR|MRS|MS|DR)\b\.?", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    return " ".join(sorted(s.split()))  # order-insensitive: "SMITH JOHN" == "JOHN SMITH"
