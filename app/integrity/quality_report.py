"""Data-quality overview: how much of the system can be trusted (dashboard 'Veri Kalitesi' tab)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _rows(conn: Connection, sql: str, **p) -> list[dict]:
    return [dict(r) for r in conn.execute(text(sql), p).mappings().all()]


def _one(conn: Connection, sql: str, **p):
    return conn.execute(text(sql), p).scalar()


def overview(conn: Connection) -> dict:
    d: dict = {}
    d["returns"] = {
        "mature_events": _one(conn, "SELECT count(*) FROM events WHERE NOT superseded AND public_date < CURRENT_DATE - 100 "
                                    "AND (ticker IS NOT NULL OR issuer_cik IS NOT NULL)"),
        "by_status": _rows(conn, "SELECT returns_status status, count(*) n FROM events WHERE NOT superseded "
                                 "AND public_date < CURRENT_DATE - 100 GROUP BY 1 ORDER BY 2 DESC"),
        "missing_by_reason": _rows(conn, "SELECT COALESCE(missing_reason, 'UNCLASSIFIED') reason, count(*) n FROM events "
                                         "WHERE NOT superseded AND returns_status = 'no_data' GROUP BY 1 ORDER BY 2 DESC"),
        "price_identity": _rows(conn, "SELECT COALESCE(price_symbol_basis, 'NOT_YET_VERIFIED') basis, count(*) n FROM events "
                                      "WHERE NOT superseded AND returns_status IN ('complete','partial') GROUP BY 1 ORDER BY 2 DESC"),
        "delisting_terminal_exits": _one(conn, "SELECT count(DISTINCT event_id) FROM event_returns WHERE exit_basis = 'delisting_terminal'"),
    }
    d["delisted"] = {
        "master_status": _rows(conn, "SELECT status, count(*) n FROM security_master GROUP BY 1 ORDER BY 2 DESC"),
        "delisting_reasons": _rows(conn, "SELECT COALESCE(delisting_reason,'-') reason, delisting_confidence confidence, count(*) n "
                                         "FROM security_master WHERE status = 'DELISTED' GROUP BY 1, 2 ORDER BY 3 DESC"),
        "events_of_delisted_issuers": _rows(conn, """
            SELECT CASE WHEN e.returns_status IN ('complete','partial') THEN 'resolved_with_prices'
                        WHEN e.missing_reason IN ('DELISTED_BANKRUPTCY','DELISTED_COMPLIANCE') THEN 'resolved_by_delisting_rule'
                        ELSE 'unresolved' END state, count(*) n
            FROM events e JOIN security_master sm ON sm.cik = e.issuer_cik
            WHERE sm.status = 'DELISTED' AND NOT e.superseded AND e.public_date < CURRENT_DATE - 100 GROUP BY 1"""),
    }
    d["tickers"] = {
        "unresolved": _rows(conn, "SELECT missing_reason reason, count(*) n FROM events WHERE NOT superseded AND missing_reason IN "
                                  "('TICKER_UNRESOLVED','TICKER_REUSED','SYMBOL_UNVERIFIED') GROUP BY 1"),
        "symbol_history_rows": _one(conn, "SELECT count(*) FROM security_symbols WHERE source <> 'sec_registry'"),
        "symbol_history_ciks": _one(conn, "SELECT count(DISTINCT cik) FROM security_symbols WHERE source <> 'sec_registry'"),
        "security_master_ciks": _one(conn, "SELECT count(*) FROM security_master"),
        "corporate_actions": _rows(conn, "SELECT action, count(*) n FROM corporate_actions GROUP BY 1 ORDER BY 2 DESC"),
        "listing_periods": _one(conn, "SELECT count(*) FROM listing_periods"),
    }
    d["news"] = {
        "timestamp_confidence": _rows(conn, "SELECT COALESCE(timestamp_confidence,'UNSCORED') c, count(*) n FROM news_items GROUP BY 1 ORDER BY 2 DESC"),
        "stories": _one(conn, "SELECT count(*) FROM news_story_clusters"),
        "items": _one(conn, "SELECT count(*) FROM news_items"),
        "syndicated_copies_merged": _one(conn, "SELECT COALESCE(sum(syndicated_count),0) FROM news_story_clusters"),
        "backfill_universe": _rows(conn, "SELECT COALESCE(payload->>'universe_quality','-') q, superseded, count(*) n FROM events "
                                         "WHERE event_type = 'MATERIAL_NEWS' AND is_backfill GROUP BY 1, 2"),
    }
    d["government"] = {
        "mapping_confidence": _rows(conn, "SELECT COALESCE(mapping_confidence,'UNMAPPED') c, count(*) n FROM government_awards GROUP BY 1 ORDER BY 2 DESC"),
    }
    d["hit_rates"] = {
        "by_status": _rows(conn, "SELECT status, count(*) n FROM metrics_quality GROUP BY 1 ORDER BY 1"),
        "metrics": _rows(conn, "SELECT metric_key, status, stats->>'unresolved_share' unresolved_share, stats->>'company_n' companies, "
                               "stats->>'unique_episodes' episodes, stats->>'company_equal_rate' rate, computed_at "
                               "FROM metrics_quality ORDER BY metric_key LIMIT 200"),
    }
    d["duplicates"] = {
        "superseded_events": _rows(conn, "SELECT COALESCE(superseded_reason,'-') reason, count(*) n FROM events WHERE superseded GROUP BY 1 ORDER BY 2 DESC"),
        "cleanup_runs": _rows(conn, "SELECT run_id, dry_run, action, count(*) n, min(created_at) at FROM data_cleanup_audit "
                                    "GROUP BY 1, 2, 3 ORDER BY 5 DESC LIMIT 30"),
    }
    d["insiders"] = {
        "attribution": _rows(conn, "SELECT COALESCE(owner_attribution,'NOT_RESCANNED') a, count(*) n FROM insider_transactions "
                                   "WHERE category = 'OPEN_MARKET_BUY' GROUP BY 1 ORDER BY 2 DESC"),
        "multi_owner_filings": _one(conn, "SELECT count(*) FROM (SELECT accession FROM insider_filing_owners GROUP BY 1 HAVING count(*) > 1) x"),
        "beneficial_links": _rows(conn, "SELECT relationship_type t, confidence c, count(*) n FROM beneficial_owner_links GROUP BY 1, 2 ORDER BY 3 DESC"),
    }
    d["congress"] = {"coverage": "HOUSE_ONLY", "senate": "SENATE_UNAVAILABLE"}
    d["oge"] = {
        "documents": _rows(conn, "SELECT doc_type, parse_status, count(*) n FROM oge_documents GROUP BY 1, 2 ORDER BY 3 DESC"),
        "transactions": _rows(conn, "SELECT parse_confidence c, count(*) n FROM oge_transactions GROUP BY 1"),
    }
    d["walkforward"] = _rows(conn, """SELECT study, fold, chosen->>'label' chosen, metrics->>'status' status,
                                             metrics->'test'->>'rate' test_rate, metrics->'test_baseline'->>'rate' baseline_rate
                                      FROM walkforward_results WHERE run_id = (SELECT run_id FROM walkforward_results ORDER BY id DESC LIMIT 1)
                                      ORDER BY study, fold""")
    d["discovery_validation"] = _rows(conn, """SELECT sample_id, level_at_sample, COALESCE(label,'UNLABELED') label, count(*) n
                                               FROM discovery_validation GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
    return d
