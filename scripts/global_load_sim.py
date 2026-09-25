"""Development-only load simulation: parse one day of TR + CA + AU volume from the real fixtures and report
CPU seconds and peak RSS. Network and DB are excluded (measured separately in the report)."""
import gzip
import resource
import sys
import time
from pathlib import Path

from app.sources.au import asx
from app.sources.ca import newswire as nw
from app.sources.tr import kap

FX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "global"
kap_pages = [gzip.decompress(p.read_bytes()).decode("utf-8", "ignore") for p in sorted(Path(sys.argv[1]).glob("*.html.gz"))]
asx_page = gzip.decompress((FX / "au/prevBusDayAnns.html.gz").read_bytes()).decode()
nw_pages = [gzip.decompress(p.read_bytes()).decode() for p in sorted((FX / "ca").glob("newswire_ca_sitemap_p*.xml.gz"))]
t0 = time.process_time()
n_kap = 0
for k in range(6):  # ~1,650 KAP pages = one day of disclosures
    for i, p in enumerate(kap_pages):
        try:
            kap.to_canonical(kap.parse_page(p, f"{k}-{i}"))
            n_kap += 1
        except Exception:
            pass
t_kap = time.process_time() - t0
t1 = time.process_time()
for _ in range(60):  # ASX full-day list every 15 minutes over the 15-hour window
    asx.dedupe([e for e in (asx.to_canonical(a) for a in asx.parse_list_page(asx_page)) if e])
t_asx = time.process_time() - t1
t2 = time.process_time()
for _ in range(144):  # newswire page 1 every 10 minutes
    [e for r in nw.parse_sitemap(nw_pages[0]) for e in nw.to_canonical(r)]
t_nw = time.process_time() - t2
print(f"KAP {n_kap} pages: {t_kap:.1f} CPU-s | ASX 60 list parses: {t_asx:.1f} CPU-s | newswire 144 parses: {t_nw:.1f} CPU-s | "
      f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
