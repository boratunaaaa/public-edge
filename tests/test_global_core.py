"""Common global layer: schema, fingerprints/source health, security master, prices, risk features, discovery,
news registry, collectors (source failure / block / rate limit)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.marketdata.provider import Bar
from app.markets import fingerprint as fpm
from app.markets.schema import FAMILIES, CanonicalEvent, content_hash


def ev(**k):
    base = dict(country="TR", exchange="XIST", source="kap", source_id="1", family="SHARE_BUYBACK", ticker="ALFAS",
                company_name="ALFA SOLAR", published_at=datetime(2026, 9, 25, 15, 11, tzinfo=timezone.utc),
                source_url="https://www.kap.org.tr/tr/Bildirim/1", title="t")
    base.update(k)
    return CanonicalEvent(**base)


# ------------------------------------------------------------------ schema
def test_required_families_exist():
    for f in ("INSIDER_BUY", "MAJOR_HOLDER_SELL", "THRESHOLD_CROSSING", "EXPORT_CONTRACT", "NEW_FACTORY", "MINING_DISCOVERY",
              "RIGHTS_ISSUE", "TRADING_RESTRICTION", "OTHER_MATERIAL_DISCLOSURE"):
        assert f in FAMILIES


def test_schema_rejects_unknown_family_country_and_naive_time():
    with pytest.raises(ValueError):
        ev(family="MOON_LANDING")
    with pytest.raises(ValueError):
        ev(country="XX")
    with pytest.raises(ValueError):
        ev(published_at=datetime(2026, 9, 25, 15, 0))


def test_schema_normalises_to_utc_and_keeps_raw():
    from zoneinfo import ZoneInfo
    e = ev(published_at=datetime(2026, 9, 25, 18, 11, tzinfo=ZoneInfo("Europe/Istanbul")), raw={"type_code": "ÖDA"})
    assert e.published_at == datetime(2026, 9, 25, 15, 11, tzinfo=timezone.utc)
    k = e.core_kwargs(date(2026, 9, 25))
    assert k["event_type"] == "TR_SHARE_BUYBACK" and k["issuer_cik"] is None and k["payload"]["raw"]["type_code"] == "ÖDA"


def test_country_event_types_never_collide_with_us_types():
    assert ev().event_type == "TR_SHARE_BUYBACK"
    assert ev(country="AU", exchange="XASX", family="INSIDER_BUY").event_type == "AU_INSIDER_BUY" != "INSIDER_OPEN_MARKET_BUY"


def test_content_hash_detects_republished_text():
    assert content_hash("Pay Geri Alım", "metin ") == content_hash("pay geri alım", "  metin")
    assert content_hash("a", "b") != content_hash("a", "c")


# ------------------------------------------------------------------ fingerprint / source health
def test_health_baseline_ok_then_format_change_is_flagged():
    h = fpm.SourceHealth("asx_list")
    fp = fpm.fingerprint(fpm.EXPECTED["asx_list"])
    assert h.observe(fp, 800, 500).status == "OK" and h.last_event
    assert h.observe(fpm.fingerprint(["ASX Code", "Date", "Headline"]), 800, 500).status == "FORMAT_CHANGED"


def test_health_detects_silent_parser_failure_and_blocks():
    h = fpm.SourceHealth("kap")
    fp = fpm.fingerprint(["a"])
    h.observe(fp, 10, 4)
    assert h.observe(fp, 10, 0).status == "DEGRADED"
    assert fpm.SourceHealth("kap").observe(None, 0, 0, blocked=True).status == "BLOCKED"


def test_health_consecutive_failures_escalate():
    h = fpm.SourceHealth("newswire_ca")
    for _ in range(2):
        h.observe(None, 0, 0, error="HTTP 503")
    assert h.status == "DEGRADED"
    assert h.observe(None, 0, 0, error="HTTP 503").status == "FAILING" and h.consecutive_failures == 3


def test_json_paths_fingerprint_matches_markit_baseline():
    import gzip
    import json
    from pathlib import Path
    p = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/global/au/markit_BHP.json.gz").read_bytes()))
    assert sorted(set(fpm.json_paths(p))) == fpm.EXPECTED["asx_markit"]


# ------------------------------------------------------------------ security master
def test_ticker_rename_and_reuse_resolution():
    from app.markets.security_master import GlobalSecurity, SecurityMaster, SymbolPeriod
    sm = SecurityMaster([GlobalSecurity("TR", "XIST", "OLDCO", "Eski A.Ş.", "TRAOLD0001", listing_date=date(2010, 1, 1))])
    sm.rename("XIST:TRAOLD0001", "NEWCO", date(2024, 6, 1))
    assert sm.resolve("XIST", "OLDCO", date(2023, 1, 1)).security_id == "XIST:TRAOLD0001"
    assert sm.resolve("XIST", "OLDCO", date(2025, 1, 1)) is None
    sm.add(GlobalSecurity("TR", "XIST", "OLDCO", "Başka A.Ş.", "TRAOTH0002",
                          historical_tickers=[SymbolPeriod("OLDCO", date(2025, 3, 1), None)]))
    assert sm.resolve("XIST", "OLDCO", date(2025, 6, 1)).company_name == "Başka A.Ş."  # reused symbol -> the NEW company
    assert sm.resolve("XIST", "OLDCO", date(2023, 1, 1)).company_name == "Eski A.Ş."
    assert sm.reuse_conflicts() == []


def test_ambiguous_symbol_is_never_guessed():
    from app.markets.security_master import GlobalSecurity, SecurityMaster, SymbolPeriod
    sm = SecurityMaster([GlobalSecurity("AU", "XASX", "ABC", "A", "A1", historical_tickers=[SymbolPeriod("ABC", date(2020, 1, 1))]),
                         GlobalSecurity("AU", "XASX", "ABC", "B", "B1", historical_tickers=[SymbolPeriod("ABC", date(2021, 1, 1))])])
    assert sm.resolve("XASX", "ABC", date(2022, 1, 1)) is None and sm.reuse_conflicts()


# ------------------------------------------------------------------ prices
def test_yahoo_symbols_per_exchange():
    from app.markets.prices import yahoo_symbol
    assert yahoo_symbol("XIST", "THYAO") == "THYAO.IS" and yahoo_symbol("XASX", "BHP") == "BHP.AX"
    assert yahoo_symbol("XTSE", "BBD.B") == "BBD-B.TO" and yahoo_symbol("XTSX", "DPF") == "DPF.V" and yahoo_symbol("XCNQ", "INNO") == "INNO.CN"


def test_zero_volume_holiday_fill_ins_are_dropped():
    from app.markets.prices import drop_synthetic_bars
    # THYAO.IS around Kurban Bayramı 2026 (real Yahoo values)
    bars = [Bar(date(2026, 5, 26), 297.5, 298.0, 295.25, 296.75, 296.75, 11744557)] + \
           [Bar(date(2026, 5, d), 296.75, 296.75, 296.75, 296.75, 296.75, 0) for d in (27, 28, 29)] + \
           [Bar(date(2026, 6, 1), 298.5, 300.5, 291.0, 291.5, 291.5, 43996715)]
    assert [b.date.day for b in drop_synthetic_bars(bars)] == [26, 1]


def test_keyed_provider_skips_gracefully_without_key(monkeypatch):
    from app.markets.prices import KeyedProvider
    monkeypatch.delenv("BIST_VENDOR_KEY", raising=False)
    p = KeyedProvider("BIST_VENDOR_KEY")
    assert not p.available and p.get_historical_prices("THYAO", date(2026, 1, 1), date(2026, 2, 1)) == []


# ------------------------------------------------------------------ TR risk features
def test_tr_risk_features_point_in_time():
    from app.markets import tr_risk
    t0 = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
    evs = [ev(family="TRADING_RESTRICTION", source_id="v1", ticker="DMRGD", published_at=t0,
              fields={"measure": "VBTS", "short_sale_ban": True, "margin_ban": True, "from": "2026-09-28", "to": "2026-10-27"}),
           ev(family="RIGHTS_ISSUE", source_id="r1", ticker="DMRGD", published_at=t0 - timedelta(days=100), fields={"rate_pct": 250.0}),
           ev(family="TRADING_RESTRICTION", source_id="late", ticker="DMRGD", published_at=t0 + timedelta(days=30),
              fields={"measure": "VBTS", "gross_settlement": True, "to": "2026-12-01"})]
    f = tr_risk.extract("DMRGD", date(2026, 9, 26), evs, free_float=0.12, adv_try=2e6)
    assert f.flags["vbts_active"] and f.flags["credit_ban"] and f.flags["short_sale_ban"]
    assert "gross_settlement" not in f.flags  # the later disclosure is in the future at as_of
    assert f.flags["large_dilution_raise"] and f.flags["low_free_float"] and f.flags["low_liquidity"]


def test_tr_limit_run_feature():
    from app.markets import tr_risk
    closes = [10.0, 11.0, 12.1, 13.31, 14.64, 14.7]
    bars = [Bar(date(2026, 9, 1) + timedelta(days=i), c, c, c, c, c, 1) for i, c in enumerate(closes)]
    assert tr_risk.extract("X", date(2026, 9, 10), [], bars=bars).flags["limit_run_days_max_60d"] == 4


# ------------------------------------------------------------------ discovery vocabulary
def test_keywords_alone_never_reach_high():
    from app.markets.discovery_vocab import discovery_features
    kw_only = [ev(family="OTHER_MATERIAL_DISCLOSURE", title="savunma sanayii ve İHA üretimi", source_id="k1")]
    r = discovery_features("TR", kw_only)
    assert r["themes"]["defense"] and r["max_level_from_keywords"] == "LOW"
    with_ev = kw_only + [ev(family="GOVERNMENT_CONTRACT", title="SSB ile sözleşme", source_id="k2")]
    assert discovery_features("TR", with_ev)["max_level_from_keywords"] == "MEDIUM"


def test_country_vocabularies_cover_requested_themes():
    from app.markets.discovery_vocab import THEMES
    assert {"defense", "energy", "battery", "solar", "chemicals", "automation", "ai_infra", "mining", "biotech", "export"} <= set(THEMES["TR"])
    assert {"uranium", "rare_earth", "lithium", "copper", "nickel", "gold", "biotech", "cleantech"} <= set(THEMES["CA"])
    assert {"rare_earth", "uranium", "lithium", "tungsten", "copper", "critical_minerals", "defense", "battery_materials"} <= set(THEMES["AU"])


def test_news_registry_official_sources_first():
    from app.markets.news_sources import official_first
    for c in ("TR", "CA", "AU"):
        assert official_first(c)[0]["tier"] == 1


# ------------------------------------------------------------------ collectors: source failure / block / rate limit
def test_collector_handles_http_failure():
    from app.markets.collector import collect_asx_list
    h = fpm.SourceHealth("asx_list")
    b = collect_asx_list(h, get=lambda url: (503, ""))
    assert b.events == [] and h.status == "DEGRADED" and h.consecutive_failures == 1


def test_collector_stops_at_kap_waf_block():
    from app.markets.collector import collect_kap
    calls = []
    h = fpm.SourceHealth("kap")

    def get(url):
        calls.append(url)
        return 666, "<html>150207-PPE13</html>"
    b = collect_kap(h, [1, 2, 3], get=get)
    assert len(calls) == 1 and h.status == "BLOCKED" and b.events == []


def test_collector_asx_on_real_page():
    import gzip
    from pathlib import Path
    from app.markets.collector import collect_asx_list
    page = gzip.decompress((Path(__file__).parent / "fixtures/global/au/prevBusDayAnns.html.gz").read_bytes()).decode()
    h = fpm.SourceHealth("asx_list", expected_fingerprint=fpm.fingerprint(fpm.EXPECTED["asx_list"]))
    b = collect_asx_list(h, get=lambda url: (200, page))
    assert b.items == 851 and len(b.events) > 300 and h.status == "OK"


def test_global_hosts_have_polite_rate_limits():
    from app import http
    assert http._bucket("https://www.kap.org.tr/tr/Bildirim/1").interval >= 2.0
    assert http._bucket("https://www.tsx.com/json/x").interval >= 6.0  # TSX robots.txt crawl-delay
    assert http._bucket("https://www.newswire.ca/sitemap-news.xml").interval >= 2.0
