import json, re, subprocess
from pathlib import Path
R=Path("reports/second_opinion"); TS="20261008T121546Z"
md=(R/f"independent_system_audit_{TS}.md").read_text()
summary=re.search(r"```\n(INDEPENDENT_SECOND_OPINION.*?)```", md, re.S).group(1)
F=lambda i,s,l,t,st,ev,ap,lk,im,rec,now,conf:dict(id=i,severity=s,layer=l,title=t,status=st,evidence=ev,affected_path=ap,likelihood=lk,impact=im,recommendation=rec,requires_code_change_now=now,confidence=conf)
findings=[
F("P1-001","P1","A/D","Live-capable urgent-exit submit path has no dry-run guard","VERIFIED","bot/taker_exit.py:1396-1617; bot/launcher.py:828,1091-1094; bot/app_config.py:980,1016; profile STOP_LOSS_ENABLED=0","dry-run with STOP_LOSS_ENABLED=1","low now / medium once stop re-enabled","real SELL with RiskEngine bypassed","assert not test_mode at single submit choke point; gate urgent exit","yes - before re-enabling stop","high"),
F("P1-002","P1","B/D","Canonical TWAP crossing/checkpoint events suppressed from 2026-10-04T18:25Z through Oct 7","VERIFIED","evidence/05_twap_by_day_type.txt, 06_tj_twap_events.txt, 07_guard_first.txt; 9352e69 diff","flip-hazard research","occurred","crossing timing unrecoverable at event resolution for Oct 5-7","reconstruct from 1 Hz snapshots with resolution label; soak-verify HEAD REQUIRED_EVENT_TYPES","no (HEAD fix exists, unverified)","high"),
F("P1-003","P1","B","Weekday/weekend inference invalid: pseudo-replication and outcome-associated exclusion","VERIFIED","evidence/13_flip_stats.txt, 14_selection_check.txt; prior report regime/...final md line 27","regime research","n/a","false regime conclusion","day-level inference, pre-registration, multi-week data","no","high"),
F("P1-004","P1","B","required_move_sigma is not a diffusion z-score (TTE-decayed sigma, floor, average variance, Binance fallback)","VERIFIED","bot/forecast_state.py:119-127; bot/live_entry_research.py:348-359; bot/spot_pricer.py:~975-990,1155-1270","difficulty bins / hazard covariates","n/a","miscalibration","add model-consistent z field","yes (research field)","high"),
F("P1-005","P1","B","freshness_clock_semantics_version not enforced by any reader; native v2 window ~23 h","VERIFIED","bot/prediction_research_snapshot.py:118; grep; evidence/11_v2_provenance.txt","research readers","medium","silent v1/v2 mixing","enforce version in readers","yes (scripts)","high"),
F("P1-006","P1","C","Maker BUY EV gate excludes adverse selection; no real fills since 2026-09-30","VERIFIED","execution/maker_engine.py:497-510,577-586; evidence/01_runs_by_day.txt","live readiness","n/a","unproven entry edge","calibrate markout from real fills; tiny live","no","high"),
F("P1-007","P1","C/D","STOP_LOSS_ENABLED=0 removes every per-position loss cap; dry-run never executes exits","VERIFIED","622e97e diff; bot/taker_exit.py:119-123,1398","live risk","high","full stake loss per market","decouple absolute max-loss breaker","yes (before live)","high"),
F("P2-001","P2","A/C","Side-invalidation counter increments twice per quote cycle","VERIFIED","run_bot.py:2276 loop / :2402 call; execution/maker_engine.py:573-639","locked side / invalidation","high","persistence threshold ~halved","time-based persistence","yes","high"),
F("P2-002","P2","D","Unbounded storage; journal >90% diagnostics","VERIFIED","evidence/05, 08, 09","disk","high","disk full / backups refused","split diagnostics DB, hard caps, rate limits","yes","high"),
F("P2-003","P2","D","HEAD backup/guard changes never ran in production; backup on same volume","VERIFIED","git log 9352e69 time; log interval_sec=900","backup","n/a","RPO unverified","soak run; off-volume backup","no","high"),
F("P2-004","P2","B","~19 s opening snapshot blind spot; 187/480 markets with >10 s gaps; 1 clean POST_B market","VERIFIED","evidence/12_manifest_summary.txt","research coverage","high","small usable sample","fix capture timing","yes","high"),
F("P2-005","P2","A","Runtime provenance not reproducible (git_dirty=true, null diff hash; no manifest before Oct 5)","VERIFIED","evidence/runs_phase.json","cohort attribution","high","cannot map runs to code","archive dirty diff per run","yes","high"),
F("P2-006","P2","A","BBO fabrication for missing/crossed sides","VERIFIED","bot/order_runtime.py:100-119","fair/econ","low (L2 gate)","quotes on fabricated fair","reject missing/crossed","yes","medium"),
F("P2-007","P2","A","Millisecond-timestamp client order ids, not idempotent","VERIFIED (code); collision INFERRED","bot/order_submission.py:523; bot/taker_exit.py:1013,1603","live orders","low","duplicate/reject","persistent uuid/sequence","yes (before live)","medium"),
F("P3-001","P3","A","Legacy LeadLagDB schema / FF compatibility residue / retired .env key","VERIFIED","evidence/18_outcome_refs.txt","maintenance","n/a","confusion","cleanup","no","high"),
F("P3-002","P3","D","Derived health is never a gate yet shows Storage CRITICAL with tradable=YES","VERIFIED","bot/research/health.py:1; terminal_bot.log","operations","n/a","misread state","document/clarify","no","high"),
F("P3-003","P3","B","BTC1S telemetry negative source_receive latency (cross-domain)","VERIFIED (log)","logs/bot/terminal_bot.log BTC1S lines","telemetry","n/a","misleading","same-source clocks","no","medium"),
]
suspected=[
dict(id="S-1",candidate_severity="P0",title="Dry-run urgent exit sells real wallet tokens rehydrated from venue reconciliation",evidence_needed="STOP_LOSS=1 run in dry-run with wallet holding current-market tokens; confirm cache.positions_open populated by reconciliation; evidence/20 shows no STARTUP_INVENTORY_REHYDRATED ever"),
dict(id="S-2",candidate_severity="P1",title="Crash after BUY submit leaves orphan GTC order; restart re-buys (guard counts only ORDER_FILLED)",evidence_needed="crash-injection test with fake venue"),
dict(id="S-3",candidate_severity="P2",title="APFS/TM snapshots retain replaced backup extents",evidence_needed="extent-level attribution or df/du deltas around snapshot create/thin excluding os.update snapshots"),
]
C=lambda s,i,f,cl,t,r:dict(sha=s,intent=i,files=f,classification=cl,tests_cover_failure=t,runtime_evidence=r)
commits=[
C("e4e87ee","init L2 retry state",["bot/adapter_overrides.py","tests/test_market_data_backpressure.py"],"STRONG_FIX","yes (3/4 fail pre-fix)","ran from 2026-10-07T13:26Z; no retry_at errors in log window"),
C("9f60f63","freshness clock domains",["bot/prediction_research_snapshot.py","bot/research/clocks.py","bot/spot_pricer.py"],"LIKELY_CORRECT","yes (22 fail pre-fix)","v2 rows from 2026-10-06T16:30:58Z; readers do not enforce"),
C("3b50637","retire FF execution authority",["bot/market_runtime.py","bot/outcome_lead_lag_exit_handoff.py","bot/settings.py","run_bot.py"],"LIKELY_CORRECT","API-only (ImportError)","no FF intents under 3b50637; observational telemetry only"),
C("33896ce","retire Outcome/Hyperliquid",["many; see git show --stat"],"PARTIALLY_VERIFIED","yes (7/7 fail pre-fix)","31-minute run only, git_dirty=true"),
C("1fa2ba0","harden backup failures",["monitoring/trade_journal_db.py"],"LIKELY_CORRECT","yes (8 fail)","final backup OK 6.16 s"),
C("b034b4e","reduce backup write amplification",["monitoring/trade_journal_db.py"],"LIKELY_CORRECT","yes (5 fail)","interval_sec=900 in log"),
C("8309152","bounded retention policy",["monitoring/storage_retention.py"],"UNVERIFIED","not run","none examined"),
C("49aa9ca","3 h scheduled rollover",["bot/launcher.py"],"LIKELY_CORRECT","yes (3 fail)","cycles 10831-10835 s"),
C("9352e69","bound research growth, 3 h backup",["bot/twap_forward_shadow.py","bot/forward_shadow.py","monitoring/trade_journal_db.py"],"RISKY","API-only (AttributeError)","never ran; still unbounded"),
C("622e97e","disable stop-loss by default",["bot/exit_engine.py","bot/taker_exit.py","config/profiles/btc15_twap_v3.env"],"RISKY","tests modified","also disables absolute max-loss breaker"),
]
K=lambda i,v,e,c:dict(id=i,verdict=v,evidence=e,counter_argument=c)
claims=[K("K1","CONFIRM","33896ce tests 7/7 fail pre-fix; no runtime import","legacy schema/compat code remain"),
K("K2","CONFIRM","evidence/21; FAST_FOLLOW_EXECUTION_ENABLED=False","PRE_A cohorts contaminated"),
K("K3","PARTIAL","9f60f63 tests fail pre-fix","readers do not enforce version; BTC1S negative latency"),
K("K4","UNRESOLVED","evidence/13,14 (prior significance claim rejected)","T-180/T-120 direction persists"),
K("K5","PARTIAL","evidence/15","uncalibrated, inconsistent definition"),
K("K6","CONFIRM","no executed stops; P1-007","shadow forensics exist"),
K("K7","PARTIAL","3 full 3 h cycles","gaps correlate with volatile markets; HEAD untested"),
K("K8","CONFIRM","code + evidence/03","stamp = delivery not book validity"),
K("K9","UNRESOLVED","prior experiment + evidence/19","os.update snapshots; free space self-recovers"),
K("K10","CONFIRM","evidence/05 (production period); HEAD fix unverified","1 Hz snapshots allow partial reconstruction"),
K("K11","REJECT","P1-002, P2-004; 57 Tier-1 markets","1 Hz snapshots approximate"),
K("K12","CONFIRM","P1-001, P1-006, P1-007","-")]
cov=Path("reports/second_opinion/work_20261008T121546Z/coverage.md").read_text()
out=dict(meta=dict(ts=TS,head=subprocess.check_output(["git","rev-parse","HEAD"]).decode().strip(),branch="codex/db-resilience-and-stoploss-priority",dirty=True,completeness="PARTIAL",auditor_incident="7 tracked test files accidentally deleted and restored byte-identically from HEAD; see notes_pass_C.md"),
 coverage=cov, findings=findings, suspected=suspected, commits=commits, claims=claims,
 verdicts=dict(CODE_READY="NO",RESEARCH_READY="NO",TINY_LIVE_READY="NO",FULL_LIVE_READY="NO"),
 next_actions=[dict(rank=i+1,action=a,blocks_tiny_live=b,blocks_research=c) for i,(a,b,c) in enumerate([
 ("Single choke-point dry-run assertion in submit_order + gate urgent exit; failing-first test","yes","no"),
 ("Decouple absolute max-loss breaker from STOP_LOSS_ENABLED","yes","no"),
 ("Split diagnostics from TradeJournal; cap/rate-limit prediction snapshots; primary free-space guard; 24 h HEAD soak","yes","yes"),
 ("Crash safety: cancel/adopt venue open orders at startup; count intents; idempotent COIDs","yes","no"),
 ("Enforce freshness v2 in all research readers","no","yes"),
 ("Fix 19 s opening blind spot; persist canonical crossings at source resolution","no","yes"),
 ("Model-consistent z (no TTE decay, TWAP-average variance, single reference)","no","yes"),
 ("Pre-register one day-level confirmatory hypothesis; collect 4-6 weeks","no","yes"),
 ("Time-based invalidation persistence + test","yes","no"),
 ("Archive dirty diff per run; non-null dirty_diff_hash","no","yes")])],
 summary=summary)
assert len([f for f in findings if f["severity"]=="P1"])==7 and len([f for f in findings if f["severity"]=="P2"])==7 and len([f for f in findings if f["severity"]=="P3"])==3
p=R/f"independent_system_audit_{TS}.json"; p.write_text(json.dumps(out,indent=1,ensure_ascii=False)); print("wrote",p)
