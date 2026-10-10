#!/usr/bin/env python3
"""Read-only, market-level first-adverse-cross sigma audit. No network/runtime execution.

Uses canonical freshness provenance, recorded shadow fills, official outcomes.
All sampled crosses are OBSERVED_GRID_APPROX; missing paths never imply no cross.
Only writes small analysis outputs in --out. Source databases use mode=ro.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import statistics as st
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.research.store import ResearchStore
from bot.research.freshness import accept_native_v2
from research_flip_analysis import MAX_GAP
from reproduce_research_iteration import verify_official_cache

BUCKETS = ('<0.5', '0.5–<1.0', '1.0–<2.0', '>=2.0')
CHECKPOINTS = (0, 5, 15, 30)

def num(v):
    try:
        return float(v) if v is not None and not isinstance(v, bool) and math.isfinite(float(v)) else None
    except (TypeError, ValueError):
        return None

def day(ts):
    return datetime.fromtimestamp(float(ts), ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d')

def bucket(v):
    return None if v is None else BUCKETS[0] if v < .5 else BUCKETS[1] if v < 1 else BUCKETS[2] if v < 2 else BUCKETS[3]

def tte_bucket(v):
    return '>300' if v > 300 else '120–300' if v > 120 else '60–120' if v > 60 else '<=60'

def pct(k, n):
    return k / n if n else None

def quantile(v, q):
    v = sorted(x for x in v if x is not None)
    if not v: return None
    i = (len(v)-1)*q; lo = int(i); hi = math.ceil(i)
    return v[lo]*(hi-i)+v[hi]*(i-lo) if lo != hi else v[lo]

def write_csv(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys or ['empty']); w.writeheader()
        for r in rows:
            w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()})

def fieldlike(k):
    return ('sigma' in k or k == 'strike_z' or k == 'required_move_z_diffusion'
            or k=='required_move_usd' or 'distance' in k or k in ('signed_spot_minus_strike','spot_minus_strike')
            or ('bps' in k and ('strike' in k or 'required_move' in k))) and not any(
                a in k for a in ('_fresh','_age','_source','_available','_enabled','_applied','_count','_window_sec'))

def definition(field):
    """Source-reviewed definitions; aliases retain their own coverage below."""
    base={'UNITS':'dimensionless annualized volatility','USES_TTE':False,'USES_VOLATILITY':True,
        'USES_DECAY':False,'FLOOR_OR_CEILING':'depends on field below',
        'REFERENCE_PRICE_SOURCE':'forecast reference: retained forecast_reference_source; research path: Chainlink raw spot, Binance fallback',
        'STRIKE_SOURCE':'runtime locked/recovered market strike; final-window remaining-average boundary when explicitly tagged',
        'KNOWN_SOURCE_SWITCHING':'research path Chainlink raw spot -> Binance WS if unavailable; forecast uses its separately recorded reference; do not equate to TWAP',
        'FRESHNESS_GUARANTEE':'native v2 component flags vs historical FRESH_ORIGINAL; absent fields are not repaired; no continuous-path guarantee',
        'TWAP_OR_TERMINAL_ASSUMPTION':'volatility input, not a cross probability'}
    raw='std(sample log returns, ddof=1) * sqrt(365*86400 / mean positive dt); past-only history window'
    scaled='raw_or_default * configured scale'
    bounded='max(floor, min(ceiling, raw_or_default * scale))'
    decay='max(floor, bounded * max(decay_min, min(1,TTE/decay_ref))) (factor rounded 4dp); disabled -> bounded'
    final='decayed sigma; optionally max with 0.6*implied_sigma, bounded by floor/ceiling when applied'
    if field=='public_proxy.safety_sigma':
        base.update(FORMULA='abs(Binance spot - proxy/canonical strike)/(spot * prior minute log-return std * sqrt(TTE/60))',
            UNITS='dimensionless distance / volatility proxy',USES_TTE=True,USES_DECAY=False,FLOOR_OR_CEILING='no sigma floor/ceiling; >=15 prior returns; uses past 30/60 minute window',
            REFERENCE_PRICE_SOURCE='Binance closed 1-minute candles',STRIKE_SOURCE='earlier public study: mostly Binance boundary proxy; six local verified strikes',
            KNOWN_SOURCE_SWITCHING='separate public-proxy dataset; not runtime-equivalent',FRESHNESS_GUARANTEE='closed bars, past-only; no subsecond freshness',TWAP_OR_TERMINAL_ASSUMPTION='endpoint proxy, not official final-window TWAP')
    elif field=='required_move_sigma' or field.endswith('required_move_sigma_to_flip'):
        base.update(FORMULA='abs(path_spot-target)/(path_spot*sigma_after_time_decay*sqrt(H/(365.25*86400)))',UNITS='dimensionless heuristic difficulty',USES_TTE=True,USES_DECAY=True,
            FLOOR_OR_CEILING='upstream sigma floor/ceiling; no direct metric cap; invalid inputs -> null',
            TWAP_OR_TERMINAL_ASSUMPTION='before final window target=strike,H=TTE; inside target=(W*K-observed integral)/remaining seconds,H=max(1,remaining); not standard normal z')
        if field!='required_move_sigma':base['FORMULA']='market-summary '+('maximum' if field.startswith('max_') else 'minimum')+' of '+base['FORMULA']
    elif field=='required_move_z_diffusion':
        base.update(FORMULA='abs(log(target/path_spot))/(raw_sigma*sqrt(h/(365.25*86400))); h=TTE-W+W/3 before window, remaining/3 inside',UNITS='dimensionless model z',USES_TTE=True,
            FLOOR_OR_CEILING='no decay/scale/floor/implied floor; positive raw sigma/spot/target/h required',
            TWAP_OR_TERMINAL_ASSUMPTION='driftless log-Brownian final arithmetic-average approximation; Phi(-z) is modeled terminal boundary probability, not touch/revert frequency')
    elif field=='strike_z':
        base.update(FORMULA='[log(spot/strike)-0.5*sigma^2*T]/[sigma*sqrt(T)], T=TTE/(365*86400)',UNITS='signed dimensionless digital z',USES_TTE=True,
            USES_DECAY='depends on supplied sigma',FLOOR_OR_CEILING='invalid inputs return 0 in legacy helper; sigma input may be default/transformed',TWAP_OR_TERMINAL_ASSUMPTION='instantaneous digital endpoint term, not final TWAP diffusion z')
    elif 'implied_sigma_floor' in field:
        base.update(FORMULA='0.6 * implied_sigma',USES_TTE=True,FLOOR_OR_CEILING='upstream implied solver range .05–5; applied final sigma has configured bounds')
    elif 'implied_sigma' in field:
        base.update(FORMULA='bisection inverse of digital_up_probability to match retained market midpoint',USES_TTE=True,FLOOR_OR_CEILING='solver range .05–5, 20 iterations, tolerance 1e-4')
    elif 'raw_realized' in field or field=='sigma_ex_market':
        base.update(FORMULA=raw,FLOOR_OR_CEILING='no vol floor/ceiling; insufficient points or zero variance -> null')
        if field=='sigma_ex_market':base['REFERENCE_PRICE_SOURCE']='Chainlink raw spot history only; path numerator may independently switch to Binance'
    elif 'time_decay_factor' in field:
        base.update(FORMULA='round(max(decay_min,min(1,TTE/decay_ref)),4)',UNITS='dimensionless multiplier',USES_TTE=True,USES_VOLATILITY=False,USES_DECAY=True,FLOOR_OR_CEILING='decay_min..1')
    elif 'after_scale' in field:base.update(FORMULA=scaled,FLOOR_OR_CEILING='no bounds at this intermediate')
    elif 'after_bounds' in field:base.update(FORMULA=bounded,FLOOR_OR_CEILING='configured floor..ceiling')
    elif 'after_time_decay' in field or 'before_implied_floor' in field:
        base.update(FORMULA=decay,USES_TTE=True,USES_DECAY=True,FLOOR_OR_CEILING='configured floor; bounded sigma upstream')
    elif 'default' in field:base.update(FORMULA='configured sigma default',USES_VOLATILITY=False,FLOOR_OR_CEILING='configuration input; downstream bounds')
    elif field in ('sigma','sigma_final','forecast_sigma_final'):
        base.update(FORMULA=final,USES_TTE=True,USES_DECAY=True,FLOOR_OR_CEILING='configured bounds and optional implied floor; old sigma field may contain fallback default')
    elif field=='required_move_usd':
        base.update(FORMULA='target - path_spot',UNITS='USD',USES_TTE=True,USES_VOLATILITY=False,FLOOR_OR_CEILING='none',TWAP_OR_TERMINAL_ASSUMPTION='target switches to remaining-average boundary in final window')
    elif field=='required_move_bps':
        base.update(FORMULA='(target-path_spot)/path_spot * 10000',UNITS='signed bps',USES_TTE=True,USES_VOLATILITY=False,FLOOR_OR_CEILING='none',TWAP_OR_TERMINAL_ASSUMPTION='target switches to remaining-average boundary in final window')
    elif 'projected_settlement' in field:
        base.update(FORMULA='(projected_flat_or_trend_TWAP-strike)/strike*10000; projected=TWAP+(flat_or_trend_spot-TWAP)*min(1,TTE/60)',UNITS='signed bps',USES_TTE=True,USES_VOLATILITY=False,FLOOR_OR_CEILING='trend movement clipped to configured trend_cap_bps; replacement fraction 0..1',TWAP_OR_TERMINAL_ASSUMPTION='first-order pressure projection; not official settlement authority')
    elif 'bps' in field:
        base.update(FORMULA='(reference-strike)/strike*10000; abs_distance_bps is absolute value',UNITS='bps',USES_VOLATILITY=False,FLOOR_OR_CEILING='none',TWAP_OR_TERMINAL_ASSUMPTION='twap_minus_strike uses official rolling TWAP; spot fields use separately recorded spot/proxy')
        if field.startswith(('min_','max_')):base['FORMULA']='market-summary '+('minimum' if field.startswith('min_') else 'maximum')+' of (reference-strike)/strike*10000'
    elif 'distance' in field or field in ('signed_spot_minus_strike','spot_minus_strike'):
        base.update(FORMULA='spot-strike (signed at entry where prefixed entry_)',UNITS='USD',USES_VOLATILITY=False,FLOOR_OR_CEILING='none',TWAP_OR_TERMINAL_ASSUMPTION='recorded entry reference; not standardized sigma')
    else:base.update(FORMULA='see source inventory; not used as primary sigma')
    return base

def main(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    provenance = Path(a.provenance)
    meta = json.loads(provenance.with_suffix('.summary.json').read_text())
    assert hashlib.sha256(provenance.read_bytes()).hexdigest() == meta['file_sha256']
    verify_official_cache(Path(meta['official_cache']))
    official = {r['market_slug']: r['outcome_used'] for r in csv.DictReader(provenance.open())
                if r['outcome_source_used']=='POLYMARKET_OFFICIAL'}
    inventory = defaultdict(lambda: {'markets':set(), 'days':set(), 'sources':set(), 'rows':0, 'min_ts':math.inf, 'max_ts':0})
    def inventory_row(r, source, slug=None, ts=None):
        slug = slug or r.get('market_slug') or r.get('slug') or r.get('row_slug')
        ts = num(ts if ts is not None else r.get('snapshot_ts') or r.get('observed_ts') or r.get('summary_ts')
                 or (num(r.get('decision_epoch_ns'))/1e9 if num(r.get('decision_epoch_ns')) is not None else None))
        if not slug or ts is None: return
        for k,v in r.items():
            if fieldlike(k) and num(v) is not None:
                x=inventory[k]; x['rows']+=1; x['markets'].add(slug);x['days'].add(day(ts));x['sources'].add(source)
                x['min_ts']=min(x['min_ts'],ts);x['max_ts']=max(x['max_ts'],ts)

    paths = defaultdict(dict); accepted=Counter(); fingerprints=[];export_cast_rows=0
    for path in sorted(Path(a.export).glob('P_paths/*/*.parquet')):
        manifest = json.loads((Path(a.export)/'manifests'/f'P_{path.parent.name}.json').read_text())
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest==manifest['files'][path.stem]['sha256'], path
        fingerprints.append({'path':str(path),'sha256':digest})
        prov='NATIVE_V2' if path.name=='paths_native_v2.parquet' else 'HISTORICAL_PRE_V2'
        for r in pq.read_table(path).to_pylist():
            inventory_row(r,str(path))
            if not path.name.startswith('paths_'): continue
            if prov=='NATIVE_V2':
                # Verified native-export manifests pin the gate's original
                # classification. Arrow serializes the version as double.
                # Normalize exactly 2.0 at this read boundary, not runtime.
                if type(r.get('freshness_clock_semantics_version')) is float and r['freshness_clock_semantics_version']==2.0:
                    r['freshness_clock_semantics_version']=2;export_cast_rows+=1
                if not accept_native_v2(r,{}):continue
            if prov=='HISTORICAL_PRE_V2' and not str(r.get('twap_recomputation_class') or '').startswith('FRESH'): continue
            r={**r,'provenance':prov}
            paths[(prov,r['market_slug'],r['run_id'])][float(r['snapshot_ts'])]=r
            accepted[prov]+=1
    store=ResearchStore(a.db)
    for run,slug,ns,r in store.rows(event_type='PREDICTION_RESEARCH_SNAPSHOT'):
        r={**r,'run_id':run,'market_slug':slug,'snapshot_ts':float(r.get('snapshot_ts') or ns/1e9)}
        inventory_row(r,str(a.db))
        if accept_native_v2(r,{}):
            r['provenance']='NATIVE_V2';key=('NATIVE_V2',slug,run)
            if r['snapshot_ts'] not in paths[key]: accepted['NATIVE_V2']+=1
            paths[key][r['snapshot_ts']]=r
    series={k:sorted(v.values(),key=lambda r:r['snapshot_ts']) for k,v in paths.items()}
    proxy=ROOT/'reports/strike_flip_risk/safety_sigma.csv'
    if proxy.is_file():
        for r in csv.DictReader(proxy.open()):
            values={('public_proxy.'+k if k=='safety_sigma' else k):num(v) for k,v in r.items()}
            inventory_row(values,str(proxy),slug=r['slug'],ts=int(r['slug'].rsplit('-',1)[-1]))

    fills=[];live_positions={};live_mark_rows=defaultdict(list)
    with ResearchStore.open_readonly(Path(a.journal)) as c:
        c.execute('PRAGMA query_only=ON')
        live_runs={r[0] for r in c.execute("SELECT run_id FROM strategy_runs WHERE mode='LIVE'")}
        for run,raw in c.execute("SELECT run_id,payload_json FROM order_events WHERE event_type='SHADOW_SIM_ENTRY_FILLED'"):
            r=json.loads(raw);fills.append({'slug':r['slug'],'run':run,'side':r['side'],
                'ts':float(r.get('filled_ts') or r['created_ts']),'cohort':'DRY_RUN_SHADOW_FILLED'})
        for ts,run,inst,coid,raw in c.execute("SELECT ts,run_id,instrument_id,client_order_id,payload_json FROM order_events WHERE event_type='ORDER_FILLED' AND side='BUY'"):
            if run not in live_runs:continue
            r=json.loads(raw);slug=r.get('slug') or r.get('market_slug')
            live_positions.setdefault(slug,{'slug':slug,'run':run,'ts':datetime.fromisoformat(ts).timestamp(),'cohort':'LIVE','instrument':inst,'coids':set()})['coids'].add(coid)
        for ts,raw in c.execute("SELECT ts,payload_json FROM order_events WHERE event_type='FILL_MARKOUT' AND side='BUY'"):
            d=json.loads(raw);slug=d.get('slug') or d.get('market_slug')
            inventory_row(d,'journal:FILL_MARKOUT',ts=datetime.fromisoformat(ts).timestamp())
            if slug in live_positions and d.get('fill_id') in live_positions[slug]['coids']:
                live_positions[slug]['side']=d.get('entry_outcome_side')
                live_positions[slug]['entry_strike']=num(d.get('entry_strike'))
        for ts,run,typ,raw in c.execute("SELECT ts,run_id,event_type,payload_json FROM strategy_events WHERE event_type IN ('LIVE_SIGNAL_COMPARE','SHADOW_SIGNAL_CANDIDATE_LIVE','MAIN_SIGNAL_CANDIDATE_LIVE','SIDE_DECISION','EXIT_POLICY_DECISION')"):
            r=json.loads(raw); inventory_row(r,'journal:'+typ,ts=datetime.fromisoformat(ts).timestamp())
            if typ=='EXIT_POLICY_DECISION' and run in live_runs:
                slug=r.get('slug') or r.get('market_slug')
                if num(r.get('net_if_exit')) is not None and num(r.get('avg_entry')) and r.get('qty'):
                    live_mark_rows[(run,slug,r.get('instrument_id'))].append({**r,'snapshot_ts':datetime.fromisoformat(ts).timestamp()})
    # Separate legacy-reference LIVE appendix: original age semantics only,
    # no promotion to canonical v2, and no sigma imputation from another tick.
    for cold_path in a.legacy_db:
        with ResearchStore.open_readonly(Path(cold_path)) as c:
            c.execute('PRAGMA query_only=ON')
            for run,slug,ms,raw in c.execute('SELECT run_id,polymarket_slug,observed_ts_ms,payload_json FROM snapshots'):
                if slug not in live_positions or run!=live_positions[slug]['run']:continue
                d=json.loads(raw);age=num(d.get('twap_age_sec'));strike=live_positions[slug].get('entry_strike')
                r={'run_id':run,'market_slug':slug,'snapshot_ts':float(d.get('observed_ts') or ms/1000),
                    'official_twap':num(d.get('twap_price')),'strike':strike,
                    'twap_fresh':bool(age is not None and 0<=age<=10 and num(d.get('twap_price')) and strike),
                    'provenance':'LEGACY_REFERENCE_ORIGINAL_AGE_ONLY','twap_age_sec':age,
                    'market_mid_up':num(d.get('up_mid'))}
                paths[('LEGACY_REFERENCE_ORIGINAL_AGE_ONLY',slug,run)][r['snapshot_ts']]=r
    series={k:sorted(v.values(),key=lambda r:r['snapshot_ts']) for k,v in paths.items()}
    # One first fill / one held side per market. Ambiguous sides excluded.
    grouped=defaultdict(list)
    for f in fills:grouped[f['slug']].append(f)
    anchors=[]
    for slug,fs in grouped.items():
        first=min(fs,key=lambda f:f['ts']);first['multiple_fill_sides']=len({f['side'] for f in fs})>1
        anchors.append(first)
    anchors+=list(live_positions.values())
    markets=[];persistence=[]
    for f in anchors:
        slug=f['slug'];end=int(slug.rsplit('-',1)[-1])+900
        row={'market_slug':slug,'cohort':f['cohort'],'run_id':f['run'],'held_side':f.get('side'),
             'entry_ts':f['ts'],'day_taipei':day(f['ts']),'official_resolution':official.get(slug),
             'cross_quality':'UNUSABLE','status':'NO_ACCEPTED_SAME_RUN_PATH'}
        if f.get('multiple_fill_sides') or f.get('side') not in ('UP','DOWN'):
            row['status']='AMBIGUOUS_HELD_SIDE';markets.append(row);continue
        candidates=[(k,s) for k,s in series.items() if k[1]==slug and k[2]==f['run']]
        if not candidates:markets.append(row);continue
        # Do not splice provenance classes across gaps or choose by outcome.
        key,s=max(candidates,key=lambda pair:sum(f['ts']-MAX_GAP<=r['snapshot_ts']<end for r in pair[1]))
        row['provenance']=key[0]
        s=[r for r in s if f['ts']-MAX_GAP<=r['snapshot_ts']<end]
        side=1 if f['side']=='UP' else -1
        def sign(r):
            if not r.get('twap_fresh') or num(r.get('official_twap')) is None or not num(r.get('strike')):return None
            d=(r['official_twap']-r['strike'])*side
            return 1 if d>0 else -1 if d<0 else 0
        prior=[r for r in s if r['snapshot_ts']<=f['ts']]
        if not prior or sign(prior[-1]) is None:
            row['status']='ENTRY_ANCHOR_MISSING_OR_STALE';markets.append(row);continue
        s=[prior[-1]]+[r for r in s if r['snapshot_ts']>f['ts']]
        i_cross=None;fail=False;strike=prior[-1]['strike'];touch=False;prev_sign=sign(prior[-1])
        row['entry_reference_favorable']=prev_sign==1
        for i in range(1,len(s)):
            if s[i]['snapshot_ts']-s[i-1]['snapshot_ts']>MAX_GAP or sign(s[i]) is None or s[i]['strike']!=strike:
                fail=True;break
            if sign(s[i])==0:touch=True
            if sign(s[i])==-1 and prev_sign==1:i_cross=i;break
            if sign(s[i])!=0:prev_sign=sign(s[i])
        if i_cross is None:
            row['status']='PRE_CROSS_PATH_CENSORED' if fail or end-s[-1]['snapshot_ts']>MAX_GAP else 'NO_OBSERVED_ADVERSE_CROSS'
            markets.append(row);continue
        cross=s[i_cross];ts=cross['snapshot_ts'];tail=s[i_cross:]
        row.update({'cross_quality':'OBSERVED_GRID_APPROX','status':'FIRST_OBSERVED_CROSS',
            'cross_ts':ts,'cross_interval_start':s[i_cross-1]['snapshot_ts'],
            'cross_interval_width_sec':ts-s[i_cross-1]['snapshot_ts'], 'touch_before_cross':touch,
            'tte_at_cross':end-ts,'tte_bucket':tte_bucket(end-ts),'day_taipei':day(ts),
            'legacy_sigma':num(cross.get('required_move_sigma')),'diffusion_z':num(cross.get('required_move_z_diffusion')),
            'legacy_bucket':bucket(num(cross.get('required_move_sigma'))),'diffusion_bucket':bucket(num(cross.get('required_move_z_diffusion'))),
            'required_move_mode':cross.get('required_move_mode'),
            'raw_twap_distance_bps':abs(cross['official_twap']-strike)/strike*1e4,
            'required_move_bps':num(cross.get('required_move_bps')),
            'final_favorable':official.get(slug)==f['side'] if slug in official else None})
        # Recorded signed required move exposes whether the sigma numerator
        # concerns the held side's adverse raw path, rather than adverse TWAP.
        target=strike if cross.get('required_move_mode')=='PRE_FINAL_STRIKE_PROXY' else num(cross.get('required_future_avg_to_flip'))
        bps=num(cross.get('required_move_bps'))
        reconstructed_spot=target/(1+bps/1e4) if target and bps is not None and 1+bps/1e4>0 else None
        row['sigma_path_spot_inferred_from_recorded_move']=reconstructed_spot
        row['sigma_numerator_adverse_to_held_side']=((reconstructed_spot-target)*side<0) if reconstructed_spot and target else None
        revert=None;first_gap=None;unbroken=True
        for j,r in enumerate(tail[1:],1):
            if r['snapshot_ts']-tail[j-1]['snapshot_ts']>MAX_GAP or sign(r) is None or r['strike']!=strike:
                unbroken=False
                if first_gap is None:first_gap=tail[j-1]['snapshot_ts']
            if sign(r)==1 and revert is None:
                revert=r['snapshot_ts'];first_revert_identifiable=unbroken
        full_tail=all(r['snapshot_ts']-p['snapshot_ts']<=MAX_GAP and sign(r) is not None and r['strike']==strike for p,r in zip(tail,tail[1:])) and end-tail[-1]['snapshot_ts']<=MAX_GAP
        row.update({'tail_complete_observed_grid':full_tail,'revert_observed':revert is not None,
            'revert_identifiable':(first_revert_identifiable if revert is not None else full_tail),
            'revert_back':True if revert is not None and first_revert_identifiable else False if revert is None and full_tail else None,
            'first_revert_sec':revert-ts if revert is not None and first_revert_identifiable else None,
            'adverse_persistence_sec':revert-ts if revert is not None and first_revert_identifiable else end-ts if revert is None and full_tail else None,
            'persistence_right_censored_at_settlement':revert is None and full_tail,
            'token_bid_at_cross':num(cross.get('best_bid_'+f['side'].lower())),
            'token_mid_at_cross':num(cross.get('market_mid_'+f['side'].lower())),
            'direct_live_net_pnl_at_cross':None})
        if f['cohort']=='LIVE':
            book_candidates=[]
            for cold_path in a.legacy_db:
                with ResearchStore.open_readonly(Path(cold_path)) as c:
                    for ns,raw in c.execute('SELECT decision_epoch_ns,payload_json FROM lead_lag_decisions WHERE decision_epoch_ns BETWEEN ? AND ? AND run_id=? AND slug=?',
                        (int((ts-1)*1e9),int(ts*1e9),f['run'],slug)):
                        q=json.loads(raw);age=num(q.get('quote_age_sec'))
                        if q.get('event_type') in ('SHADOW_BBO_SNAPSHOT','SHADOW_BBO_MATERIAL_CHANGE') and q.get('instrument_id')==f['instrument'] and age is not None and 0<=age<=2 and num(q.get('best_bid')) is not None:
                            book_candidates.append({**q,'snapshot_ts':ns/1e9})
            if book_candidates:
                q=max(book_candidates,key=lambda q:q['snapshot_ts']);row['token_bid_at_cross']=q['best_bid']
                row['quote_mark_age_sec']=ts-q['snapshot_ts'];row['quote_quality']='RECORDED_SAME_INSTRUMENT_PRIOR_WITHIN_1S_ORIGINAL_FRESHNESS'
            marks=[m for m in live_mark_rows[(f['run'],slug,f['instrument'])] if 0<=ts-m['snapshot_ts']<=1]
            if marks:
                m=max(marks,key=lambda m:m['snapshot_ts']);row['direct_live_net_pnl_at_cross']=m['net_if_exit'];row['pnl_mark_age_sec']=ts-m['snapshot_ts']
                row['token_bid_at_cross']=num(m.get('best_bid'));row['pnl_mark_quality']='DIRECT_RECORDED_PRIOR_WITHIN_1S'
        markets.append(row)
        for checkpoint in CHECKPOINTS:
            target=ts+checkpoint; upto=[r for r in tail if r['snapshot_ts']<=target]
            br=[r for r in tail if r['snapshot_ts']>=target]
            known=(target<end and upto and br and sign(br[0]) is not None and br[0]['strike']==strike
                and sign(upto[-1])==sign(br[0])
                and br[0]['snapshot_ts']-upto[-1]['snapshot_ts']<=MAX_GAP
                and target-upto[-1]['snapshot_ts']<=MAX_GAP and br[0]['snapshot_ts']-target<=MAX_GAP
                and all(sign(r) is not None and r['strike']==strike and r['snapshot_ts']-p['snapshot_ts']<=MAX_GAP for p,r in zip(upto,upto[1:])))
            still=bool(known and all(sign(r)==-1 for r in upto) and sign(br[0])==-1)
            # Bracketing is retrospective and not a deployable decision clock.
            later=[r for r in tail if r['snapshot_ts']>target and sign(r)==1]
            later_known=full_tail or bool(later and (first_gap is None or later[0]['snapshot_ts']<=first_gap))
            persistence.append({**{k:row.get(k) for k in ('market_slug','cohort','provenance','cross_quality','day_taipei','legacy_bucket','diffusion_bucket','final_favorable')},
                'checkpoint_sec':checkpoint,'checkpoint_identifiable':bool(known),
                'still_adverse_unbroken_observed':still if known else None,
                'later_revert':bool(later) if later_known else None})

    def summarize(rs):
        settled=[r for r in rs if r['final_favorable'] is not None];rv=[r for r in rs if r['revert_back'] is not None]
        times=[r['first_revert_sec'] for r in rv if r['revert_back']]
        return {'unique_markets':len(rs),'unique_days':len({r['day_taipei'] for r in rs}),
            'final_favorable_count':sum(r['final_favorable'] for r in settled),'final_adverse_count':sum(not r['final_favorable'] for r in settled),
            'settlement_denominator':len(settled),'final_adverse_rate':pct(sum(not r['final_favorable'] for r in settled),len(settled)),
            'final_favorable_rate':pct(sum(r['final_favorable'] for r in settled),len(settled)),
            'revert_count':sum(r['revert_back'] for r in rv),'no_revert_count':sum(not r['revert_back'] for r in rv),
            'revert_denominator':len(rv),'unknown_revert_count':len(rs)-len(rv),'revert_back_rate':pct(sum(r['revert_back'] for r in rv),len(rv)),
            'median_revert_sec':quantile(times,.5),'p25_revert_sec':quantile(times,.25),'p75_revert_sec':quantile(times,.75),
            'median_tte_sec':quantile([r['tte_at_cross'] for r in rs],.5),
            'median_adverse_persistence_observed_sec':quantile([r['adverse_persistence_sec'] for r in rs],.5),
            'settlement_censored_persistence_count':sum(r['persistence_right_censored_at_settlement'] for r in rs),
            'median_raw_twap_distance_bps':quantile([r['raw_twap_distance_bps'] for r in rs],.5),
            'interpretation':'UNDERPOWERED' if len(rs)<20 or len({r['day_taipei'] for r in rs})<3 else 'DESCRIPTIVE_ONLY'}
    usable=[r for r in markets if r['cross_quality']!='UNUSABLE']
    bs=[];tt=[];ps=[]
    strata=sorted({(r['cohort'],r['provenance'],r['cross_quality']) for r in usable})
    for cohort,prov,quality in strata:
        for field,bkey in [('legacy_sigma','legacy_bucket'),('diffusion_z','diffusion_bucket')]:
            for b in BUCKETS:
                rs=[r for r in usable if (r['cohort'],r['provenance'],r['cross_quality'])==(cohort,prov,quality) and r[bkey]==b]
                prefix={'cohort':cohort,'provenance':prov,'cross_quality':quality,'field':field,'sigma_bucket':b}
                bs.append({**prefix,**summarize(rs)})
                for t in ('>300','120–300','60–120','<=60'):
                    tt.append({**prefix,'tte_bucket':t,**summarize([r for r in rs if r['tte_bucket']==t])})
                for cp in CHECKPOINTS:
                    selected=[p for p in persistence if (p['cohort'],p['provenance'],p['cross_quality'])==(cohort,prov,quality) and p[bkey]==b and p['checkpoint_sec']==cp]
                    still=[p for p in selected if p['still_adverse_unbroken_observed'] is True]
                    settled=[p for p in still if p['final_favorable'] is not None];reverted=[p for p in still if p['later_revert'] is not None]
                    ps.append({**prefix,'checkpoint_sec':cp,'first_cross_markets':len(selected),
                        'checkpoint_identifiable_markets':sum(p['checkpoint_identifiable'] for p in selected),
                        'still_adverse_markets':len(still),'unique_days':len({p['day_taipei'] for p in still}),
                        'final_adverse_rate':pct(sum(not p['final_favorable'] for p in settled),len(settled)),
                        'later_revert_denominator':len(reverted),'later_favorable_recross_rate':pct(sum(p['later_revert'] for p in reverted),len(reverted))})
    defs=[]
    for field,x in sorted(inventory.items()):
        defs.append({'FIELD':field,**definition(field),'NUMBER_OF_MARKETS':len(x['markets']),'NUMBER_OF_DAYS':len(x['days']),
            'NONNULL_ROWS':x['rows'],'HISTORICAL_COVERAGE':[datetime.fromtimestamp(x['min_ts'],ZoneInfo('Asia/Taipei')).isoformat(),datetime.fromtimestamp(x['max_ts'],ZoneInfo('Asia/Taipei')).isoformat()],
            'SOURCE_FILE_OR_EVENT':sorted(x['sources'])})
    live=[r for r in markets if r['cohort']=='LIVE'];known=[r for r in live if r.get('direct_live_net_pnl_at_cross') is not None]
    summary={'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'expected_head':'14ec69f','LEGACY_SIGMA_RUNTIME_EQUIVALENT':'PARTIAL','DIFFUSION_Z_RUNTIME_EQUIVALENT':'YES',
        'cohort_counts':dict(Counter(r['cohort'] for r in markets)),'status_counts':dict(Counter(r['status'] for r in markets)),
        'cross_quality_counts':dict(Counter(r['cross_quality'] for r in markets)),
        'usable_cross_markets':len(usable),'usable_cross_days':len({r['day_taipei'] for r in usable}),
        'coverage_by_provenance':[{ 'provenance':p,'cross_markets':len(rs),'days':len({r['day_taipei'] for r in rs}),
            'legacy_sigma_markets':sum(r['legacy_sigma'] is not None for r in rs),'diffusion_z_markets':sum(r['diffusion_z'] is not None for r in rs)}
            for p in ('HISTORICAL_PRE_V2','NATIVE_V2','LEGACY_REFERENCE_ORIGINAL_AGE_ONLY') for rs in [[r for r in usable if r['provenance']==p]]],
        'CROSS_BEFORE_MINUS2_IDENTIFIABLE_MARKETS':len(known),
        'CROSS_ALREADY_BELOW_MINUS2':sum(r['direct_live_net_pnl_at_cross']<=-2 for r in known),
        'CROSS_STILL_ABOVE_MINUS2':sum(r['direct_live_net_pnl_at_cross']>-2 for r in known),
        'live_observed_crosses_without_sigma':sum(r['cross_quality']=='OBSERVED_GRID_APPROX' and r.get('legacy_sigma') is None for r in live),
        'live_observed_cross_pnl_unknown':sum(r['cross_quality']=='OBSERVED_GRID_APPROX' and r.get('direct_live_net_pnl_at_cross') is None for r in live),
        'UNKNOWN':len(live)-len(known), 'live_pnl_unknown_definition':'all LIVE buy markets lacking usable sigma-cross + synchronized direct PnL, including no accepted path',
        'field_inventory':defs,'accepted_snapshot_rows':dict(accepted),'input_fingerprints':fingerprints,
        'verified_native_export_float_version_cast_rows':export_cast_rows,
        'settings':{'max_gap_sec':MAX_GAP,'persistence_checkpoints':CHECKPOINTS,'unit':'one market, first recorded fill and first usable adverse crossing','day_timezone':'Asia/Taipei'},
        'bucket_summary':bs,'persistence_summary':ps,'tte_summary':tt}
    for name,rows in [('market_level',markets),('bucket_summary',bs),('persistence_summary',ps),('tte_summary',tt)]:write_csv(out/(name+'.csv'),rows)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k not in ('field_inventory','input_fingerprints','bucket_summary','persistence_summary','tte_summary')},ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--export',default='data/research_export');p.add_argument('--db',default='data/research/twap_forward_shadow.db')
    p.add_argument('--journal',default='logs/trade_journal.db');p.add_argument('--provenance',default='data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv')
    p.add_argument('--legacy-db',action='append',default=[],help='verified closed legacy staging DB; LIVE reference appendix only')
    p.add_argument('--out',default='reports/sigma_adverse_cross_audit');main(p.parse_args())
