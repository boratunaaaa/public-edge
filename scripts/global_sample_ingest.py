"""Development-only: ingest the real public samples into a DEV database through the shared core and export JSONL.

usage: DATABASE_URL=<dev db> python scripts/global_sample_ingest.py <kap_pages_dir> <out_dir>
Never point DATABASE_URL at production (the script refuses a database named public_edge).
"""
import gzip
import json
import os
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine

from app.db.models import metadata
from app.db.store import create_event
from app.markets.calendars import calendar_for
from app.sources.au import asx
from app.sources.ca import newswire as nw
from app.sources.tr import kap

url = os.environ["DATABASE_URL"]
if url.rstrip("/").endswith("/public_edge"):
    sys.exit("refusing to write samples into the production database")
kap_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
FX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "global"
tr = []
for p in sorted(kap_dir.glob("*.html.gz")):
    try:
        tr += kap.to_canonical(kap.parse_page(gzip.decompress(p.read_bytes()).decode("utf-8", "ignore"), p.name.split(".")[0]))
    except (kap.KapBlocked, ValueError):
        pass
tr = kap.dedupe(tr)
au = [e for e in (asx.to_canonical(a) for a in asx.parse_list_page(gzip.decompress((FX / "au/prevBusDayAnns.html.gz").read_bytes()).decode())) if e]
for p in sorted((FX / "au").glob("markit_*.json.gz")):
    code = p.name.split("_")[1].split(".")[0]
    au += [e for e in (asx.to_canonical(a) for a in asx.parse_markit(code, json.loads(gzip.decompress(p.read_bytes())))) if e]
au = asx.dedupe(au)
ca = [e for p in sorted((FX / "ca").glob("newswire_ca_sitemap_p*.xml.gz"))
      for r in nw.parse_sitemap(gzip.decompress(p.read_bytes()).decode()) for e in nw.to_canonical(r)]
eng = create_engine(url)
metadata.create_all(eng)
stats = {}
with eng.begin() as c:
    for name, evs in (("TR", tr), ("CA", ca), ("AU", au)):
        new = sum(1 for e in evs if create_event(c, **e.core_kwargs(calendar_for(e.exchange).local_date(e.published_at))))
        (out / f"{name.lower()}_canonical_events.jsonl").write_text("\n".join(json.dumps(e.to_json(), ensure_ascii=False, default=str) for e in evs))
        stats[name] = {"events": len(evs), "stored_new": new, "families": dict(Counter(e.family for e in evs).most_common())}
print(json.dumps(stats, ensure_ascii=False, indent=1))
