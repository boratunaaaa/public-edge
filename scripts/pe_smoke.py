"""Production smoke tests (read-only). Run inside a backfill container:
    docker compose run --rm --no-deps -T backfill python scripts/pe_smoke.py
Exit code 0 only when every check passes; prints one line per check."""
from __future__ import annotations

import os
import sys
from datetime import date

from sqlalchemy import text

from app.db.session import get_engine

HEALTH = {"HEALTHY", "STALE", "DEGRADED", "BLOCKED", "SCHEMA_CHANGED", "AUTH_REQUIRED", "DISABLED", "UNKNOWN", "RETIRED"}
V2_SOURCES = ("finra_short_volume", "sec_ftd", "us_clinical_trials", "us_openfda", "us_cftc_cot", "ca_canadabuys",
              "ca_boc_valet", "tr_spk_bulletin")
BLOCKED = ("sedi", "ciro_short", "ca_lobbying", "senate_lda")
AUTH = ("mkk_kap", "fred_alfred", "uspto_patents")


def main() -> int:
    eng = get_engine()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    with eng.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        v = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        check("migration_0014", v == "0014", v)
        n, nulls = c.execute(text("SELECT count(*), count(*) FILTER (WHERE available_at IS NULL) FROM events")).one()
        check("available_at_populated", nulls == 0, f"{n} events, {nulls} null")
        bad = c.execute(text("""SELECT count(*) FROM events WHERE date_quality = 'exact' AND public_at IS NOT NULL
                                AND available_at <> public_at""")).scalar()
        check("available_at_exact_rule", bad == 0, f"{bad} mismatches")
        early = c.execute(text("""SELECT count(*) FROM event_returns r JOIN events e ON e.id = r.event_id
                                  WHERE r.entry_date < (e.available_at AT TIME ZONE 'America/New_York')::date""")).scalar()
        check("no_entry_before_available_at", early == 0, f"{early} rows")
        hs = {r[0]: r[1] for r in c.execute(text("SELECT source, health_status FROM source_health"))}
        check("health_vocabulary", set(hs.values()) <= HEALTH, str(sorted(set(hs.values()) - HEALTH)))
        check("v2_sources_reporting", all(s in hs for s in V2_SOURCES), str([s for s in V2_SOURCES if s not in hs]))
        check("blocked_sources_marked", all(hs.get(s) == "BLOCKED" for s in BLOCKED), str({s: hs.get(s) for s in BLOCKED}))
        check("auth_required_marked", all(hs.get(s) in ("AUTH_REQUIRED", "HEALTHY", "STALE") for s in AUTH), str({s: hs.get(s) for s in AUTH}))
        ds = c.execute(text("SELECT count(DISTINCT status) FROM source_daily_status WHERE day = CURRENT_DATE")).scalar()
        check("daily_status_recorded", ds >= 1, f"{ds} statuses today")
        rl = {r[0] for r in c.execute(text("SELECT key FROM rate_limits"))}
        check("shared_rate_limits", {"sec"} <= rl, str(sorted(rl)))
        check("shared_rate_limit_enabled", os.environ.get("PE_SHARED_RATELIMIT") == "1", os.environ.get("PE_SHARED_RATELIMIT", ""))
        g = dict(c.execute(text("SELECT country, count(*) FROM global_securities GROUP BY 1")).all())
        check("global_master_countries", {"US", "TR", "CA"} <= set(g), str(g))
        amb = c.execute(text("""SELECT count(*) FROM (SELECT p.symbol FROM security_symbol_periods p
                                JOIN global_securities g USING (security_id) WHERE g.country = 'TR'
                                AND (p.valid_to IS NULL OR p.valid_to > CURRENT_DATE) GROUP BY p.symbol
                                HAVING count(DISTINCT p.security_id) > 1) x""")).scalar()
        check("tr_codes_unique_today", amb == 0, f"{amb} ambiguous")
        from app.markets import master_db
        check("resolve_is_period_bound", master_db.resolve(c, "ZZZZZZ", date.today()) is None, "unknown symbol -> None")
        books = dict(c.execute(text("SELECT book, count(*) FROM paper_accounts WHERE active GROUP BY 1")).all())
        check("research_books_exist", books.get("RESEARCH", 0) == 5 and books.get("MAIN", 0) == 5, str(books))
        check("research_mode_on", os.environ.get("PAPER_RESEARCH_MODE") == "1", os.environ.get("PAPER_RESEARCH_MODE", ""))
        main_open = c.execute(text("""SELECT count(*) FROM paper_orders o JOIN paper_accounts a ON a.id = o.account_id
                                      JOIN proposed_trades p ON p.id = o.proposal_id
                                      WHERE a.book = 'MAIN' AND p.proposal_created_at > '2026-09-28'""")).scalar()
        check("main_books_closed", main_open == 0, f"{main_open} MAIN orders since V2")
        nr = c.execute(text("""SELECT count(*) FROM proposed_trades p JOIN events e ON e.id = p.event_id
                               WHERE e.needs_review AND p.proposal_created_at > '2026-09-28'""")).scalar()
        check("needs_review_never_proposed", nr == 0, f"{nr}")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
