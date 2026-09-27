# V2 US fixtures (real public records, trimmed)

| file | source | retrieved | trimming |
|---|---|---|---|
| finra_CNMSshvol20260925_head.txt | https://cdn.finra.org/equity/regsho/daily/CNMSshvol20260925.txt | 2026-09-28 | first 300 rows |
| sec_cnsfails202608b_head.txt | https://www.sec.gov/files/data/fails-deliver-data/cnsfails202608b.zip | 2026-09-28 | first 300 rows |
| 13f_2026q2_mini.zip | https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip | 2026-09-28 | 3 managers (CIK 0002019841, 0002055400, 0001948391), SUBMISSION/COVERPAGE/SUMMARYPAGE/INFOTABLE rows unchanged |
| 13f_2026q1_mini.zip | https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip | 2026-09-28 | same 3 managers |
| ctgov_moderna_page.json | https://clinicaltrials.gov/api/v2/studies?query.spons=Moderna&pageSize=5&sort=LastUpdatePostDate:desc | 2026-09-28 | none |
| ctgov_pfizer_results_2026-08.json | https://clinicaltrials.gov/api/v2/studies?query.lead=Pfizer&filter.advanced=AREA[ResultsFirstPostDate]RANGE[2026-08-01,MAX]&pageSize=4 | 2026-09-28 | none |
| ctgov_pfizer_terminated.json | https://clinicaltrials.gov/api/v2/studies?query.lead=Pfizer&filter.overallStatus=TERMINATED&pageSize=2 | 2026-09-28 | none |
| openfda_drugsfda_orig_2026-08.json | https://api.fda.gov/drug/drugsfda.json?search=submissions.submission_status_date:[20260801 TO 20260925] AND submissions.submission_type:ORIG&limit=3 | 2026-09-28 | none |
| cftc_legacy_crude_2026-09-15.json | https://publicreporting.cftc.gov/resource/6dca-aqww.json (report 2026-09-15, CRUDE OIL) | 2026-09-28 | none |
