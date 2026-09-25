"""Status of the other Turkish primary sources (checked 2026-09-26 from the production server).

MKK (mkk.com.tr): site reachable; no public per-company disclosure feed (e-Yönet / investor statistics are
licensed services) -> LICENSED. KAP runs on MKK infrastructure: the licensed KAP data service is the production path.
SPK (spk.gov.tr/spk-bultenleri): weekly bulletin PDFs reachable -> CANDIDATE (capital increase approvals, trading
bans, administrative fines); parser not built tonight.
Company IR pages: mirror KAP; used only as a fallback for attachments -> CANDIDATE.
TEFAS: the fund platform rejects the production server ("Request Rejected") -> BLOCKED (funds are out of scope).
"""
STATUS = {"KAP": "PARTIAL", "MKK": "LICENSED", "SPK": "CANDIDATE", "COMPANY_IR": "CANDIDATE", "TEFAS": "BLOCKED"}
