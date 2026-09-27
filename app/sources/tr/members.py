"""BIST company list from KAP's public page (https://www.kap.org.tr/tr/bist-sirketler - server rendered; the JSON API
behind the WAF is NOT used). The MKK member OID is the stable issuer id; each BIST code of a member becomes one
security with a symbol period opened at first sight (history before first sight is unknown -> never assumed)."""
from __future__ import annotations

import json
import re
from datetime import date

from app import http

URL = "https://www.kap.org.tr/tr/bist-sirketler"
OBJ = re.compile(r'\{\\"mkkMemberOid\\":\\"[^{}]*?\}')


def parse(page: str) -> list[dict]:
    out = []
    for o in OBJ.findall(page):
        try:
            r = json.loads(o.replace('\\"', '"'))
        except ValueError:
            continue
        codes = [c.strip() for c in (r.get("stockCode") or "").split(",") if c.strip()]
        out.append({"oid": r["mkkMemberOid"], "name": r.get("kapMemberTitle"), "codes": codes, "type": r.get("kapMemberType")})
    return out


def sync(conn, rows: list[dict], as_of: date) -> int:
    from sqlalchemy import text

    from app.markets import master_db
    n = 0
    for r in rows:
        for code in r["codes"]:
            sid = f"TR:XIST:{code}"
            master_db.upsert_security(conn, security_id=sid, country="TR", exchange="XIST", issuer_id=r["oid"], issuer_name=r["name"],
                                      local_id=r["oid"], security_type="COMMON", currency="TRY", tradeability_status="ACTIVE",
                                      identity_confidence="HIGH", evidence=[{"source": "kap_bist_sirketler", "as_of": as_of.isoformat()}])
            if not conn.execute(text("SELECT 1 FROM security_symbol_periods WHERE security_id = :s AND symbol = :c"),
                                {"s": sid, "c": code}).first():
                master_db.add_symbol_period(conn, sid, code, as_of, None, "kap_bist_sirketler", exchange="XIST", confidence="HIGH")
            n += 1
    return n


def fetch() -> list[dict]:
    return parse(http.get(URL).text)
