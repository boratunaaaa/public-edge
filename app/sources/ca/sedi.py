"""SEDI (System for Electronic Disclosure by Insiders) - BLOCKED_SOURCE.

2026-09-26: www.sedi.ca search pages answer a Radware Bot Manager block page to automated clients (production
server), and SEDAR+ likewise. PUBLIC EDGE does not bypass bot protection. No real SEDI record could be
collected, so no parser is claimed as tested; the canonical mapping below is the contract a licensed/authorised
feed must fill. Canadian insider coverage: NONE.
"""
STATUS = "BLOCKED_SOURCE"
REQUIRED_FIELDS = ("insider_name", "relationship", "issuer", "security", "transaction_date", "filing_date", "nature_code",
                   "quantity", "unit_price", "balance_after")
# SEDI nature-of-transaction codes relevant to open-market insider signals
OPEN_MARKET_CODES = {"10": "acquisition or disposition in the public market"}


def family_for(nature_code: str, quantity: float) -> str | None:
    """INSIDER_BUY / INSIDER_SELL only for open-market transactions (code 10); grants/exercises are not signals."""
    if nature_code != "10" or not quantity:
        return None
    return "INSIDER_BUY" if quantity > 0 else "INSIDER_SELL"
