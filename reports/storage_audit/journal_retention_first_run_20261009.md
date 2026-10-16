# Journal retention first run (2026-10-09)

commit 9df646b; bot stopped; no network; no push

## Real apply
{"status": "ok", "apply": true, "cutoff_day": "2026-09-25", "archived_days": ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"], "deleted_rows": 429799, "errors": [], "mirror": {"status": "disabled", "copied": 0}}


## One-time VACUUM
{"bytes_before": 3205967872, "bytes_after": 2345553920, "integrity_check": "ok", "duration_sec": 67.0, "at_utc": "2026-10-09T13:52:55.603628+00:00"}


## Free space (df -k, KB avail)
before real run: 19759976
before VACUUM: 18895908
after VACUUM + scratch cleanup: 19750176
