SELECT substr(started_at,1,10) d, mode, test_mode, COUNT(*) n, MIN(started_at), MAX(COALESCE(ended_at,'')) FROM strategy_runs WHERE started_at>='2026-09-25' GROUP BY d,mode,test_mode ORDER BY d;
