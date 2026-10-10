import json,csv,sqlite3,statistics,collections,hashlib,lzma,random
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path('/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main');OUT=ROOT/'reports/consolidated_breaker_value_audit';OUT.mkdir(exist_ok=True)
TZ=ZoneInfo('Asia/Taipei')
def readjson(raw):return json.loads(raw or '{}')
def ts(t):return datetime.fromisoformat(t).timestamp()
def csvwrite(name,rr):
 with (OUT/name).open('w',newline='') as f:
  keys=list(dict.fromkeys(k for x in rr for k in x));w=csv.DictWriter(f,keys);w.writeheader()
  for x in rr:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in x.items()})
j=sqlite3.connect(f'file:{ROOT}/logs/trade_journal.db?mode=ro',uri=True);j.row_factory=sqlite3.Row
runs={x['run_id']:dict(x) for x in j.execute('select * from strategy_runs')}
orders=[dict(x) for x in j.execute("select * from order_events where event_type in ('ORDER_FILLED','ORDER_TAKER_EXIT_SUBMIT','ORDER_SUBMIT','FILL_MARKOUT') order by id")]
for o in orders:o['p']=readjson(o['payload_json'])
mark={}
for o in orders:
 if o['event_type']=='FILL_MARKOUT':mark.setdefault(o['p'].get('fill_id'),o['p'])
cache={};cache_paths=[ROOT/'data/research_export/official_resolution/official_resolutions_20261009T041657Z.json']+list((ROOT/'reports/historical_early_warning_audit/official_resolution_cache').glob('*.json'))+[ROOT/'reports/post_live_deterioration_audit_20261010/official_resolution_verification.json']
for path in cache_paths:
 for x in readjson(path.read_text()).get('markets',[]):
  if x.get('official_outcome') in ['UP','DOWN']:cache[x['slug']]=dict(x,cache_file=str(path))
condition={x.get('condition_id'):slug for slug,x in cache.items() if x.get('condition_id')}
pos={};excluded=[]
for o in orders:
 if o['event_type']!='ORDER_FILLED' or o['side']!='BUY' or runs.get(o['run_id'],{}).get('mode')!='LIVE':continue
 p=o['p'];slug=p.get('slug') or p.get('market_slug');m=mark.get(o['client_order_id'],{});side=m.get('entry_outcome_side')
 if side not in ['UP','DOWN']:side=(p.get('decision_trace') or {}).get('held_side')
 if side not in ['UP','DOWN']:
  # Position-opened telemetry is authoritative held-side evidence for recent fills.
  hit=j.execute("select payload_json from strategy_events where run_id=? and event_type='STOP_TIMING_POSITION_OPENED'",(o['run_id'],)).fetchall()
  side=next((readjson(x[0])['held_side'] for x in hit if readjson(x[0]).get('slug')==slug and readjson(x[0]).get('instrument_id')==o['instrument_id']),None)
 cond=(o['instrument_id'] or '').split('-')[0]
 canonical=condition.get(cond,slug)
 if not canonical or side not in ['UP','DOWN']:excluded.append({'reason':'BUY_IDENTITY_UNKNOWN','order_id':o['id']});continue
 key=(canonical,side);pos.setdefault(key,{'slug':canonical,'side':side,'buys':[],'sells':[],'condition':cond})['buys'].append(o)
# Map SELL through submit identity + BUY condition; never use faulty filled-token id alone.
submit={o['client_order_id']:o for o in orders if o['event_type'] in ['ORDER_TAKER_EXIT_SUBMIT','ORDER_SUBMIT']}
identity_corrections=[]
for o in orders:
 if o['event_type']!='ORDER_FILLED' or o['side']!='SELL' or runs.get(o['run_id'],{}).get('mode')!='LIVE':continue
 if not o['price'] or o['price']<=0:
  excluded.append({'reason':'ZERO_PRICE_SYNTHETIC_FILL_NOT_CASH_TRADE','order_id':o['id'],'slug':o['p'].get('slug')});continue
 source=submit.get(o['client_order_id'],{});inst=source.get('instrument_id') or source.get('p',{}).get('instrument_id') or o['instrument_id'];cond=(inst or '').split('-')[0]
 candidates=[p for p in pos.values() if p['condition']==cond]
 lifecycle=o['p'].get('entry_client_order_id')
 if len(candidates)>1:candidates=[p for p in candidates if any(b['client_order_id']==lifecycle or b['instrument_id']==inst for b in p['buys'])]
 if len(candidates)!=1:excluded.append({'reason':'UNPAIRED_OR_AMBIGUOUS_SELL','order_id':o['id'],'slug':o['p'].get('slug'),'condition':cond});continue
 p=candidates[0];p['sells'].append(o)
 if o['instrument_id']!=p['buys'][0]['instrument_id'] or o['p'].get('slug')!=p['slug']:identity_corrections.append({'fill_id':o['id'],'original_slug':o['p'].get('slug'),'canonical_slug':p['slug'],'original_inst':o['instrument_id'],'canonical_buy_inst':p['buys'][0]['instrument_id'],'submit_coid':o['client_order_id']})
missing=[p['slug'] for p in pos.values() if p['slug'] not in cache]
(OUT/'missing_official_slugs.json').write_text(json.dumps(missing));print('MISSING',missing)
assert not missing,'fetch missing official outcomes before interpretation'
strategy=[dict(x) for x in j.execute("select ts,run_id,event_type,payload_json from strategy_events where event_type in ('MARKET_SETTLEMENT','MARKET_CYCLE_PNL','STOP_TIMING_POSITION_OPENED','STOP_TIMING_COMPONENT_FIRST_TRUE','STOP_TIMING_BREAKER_FIRST_ELIGIBLE','STOP_TIMING_POSITION_SETTLEMENT','EXIT_POLICY_DECISION') order by id")]
for x in strategy:x['p']=readjson(x['payload_json'])
hist={x['market']:x for x in csv.DictReader((ROOT/'reports/historical_early_warning_audit/position_timeline.csv').open())}
latest=readjson((ROOT/'reports/post_live_deterioration_audit_20261010/summary.json').read_text());latestpos={x['slug']:x for x in latest['positions']}
allp=[];timing=[];features=[]
for key,p in pos.items():
 bs=p['buys'];ss=p['sells'];slug=p['slug'];first=min(ts(b['ts']) for b in bs);rawqty=sum(b['qty'] for b in bs);sharefee=sum(b['p'].get('effective_fee_shares') or 0 for b in bs);qty=rawqty-sharefee;cost=sum(b['price']*b['qty'] for b in bs);entryfee=sum(b['p'].get('effective_fee_usdc') or 0 for b in bs);sellfee=sum(o['p'].get('effective_fee_usdc') or 0 for o in ss);sold=sum(o['qty'] for o in ss);proceeds=sum(o['price']*o['qty'] for o in ss);residual=qty-sold
 if residual < -1e-5:excluded.append({'reason':'OVERSELL_RECONSTRUCTION','slug':slug,'residual':residual});continue
 residual=max(0,residual);winner=cache[slug]['official_outcome']==p['side'];pay=int(winner);hold=qty*pay-cost-entryfee;actual=proceeds+residual*pay-cost-entryfee-sellfee
 sellkinds=[];breakers=[]
 for o in ss:
  sub=submit.get(o['client_order_id'],{});sp=sub.get('p',{});coid=o['client_order_id'] or ''
  if coid.startswith('BTC-15M-MAKER-SELL-'):kind='MAKER_TP'
  elif sp.get('decision_reason')=='absolute_max_loss_breaker' or str(sp.get('decision_reason','')).startswith('catastrophic_stop_loss'):kind='BREAKER';breakers.append(sub)
  elif 'TAKER-EXIT' in coid or 'URGENT' in coid or 'RECOVERY-PASSIVE' in coid:kind='OTHER_PROTECTIVE'
  else:kind='UNKNOWN'
  sellkinds.append(kind)
 exit_type='NO_EXIT_HOLD' if not ss else 'TP_EXIT' if set(sellkinds)=={'MAKER_TP'} else 'BREAKER_EXIT' if set(sellkinds)=={'BREAKER'} else 'OTHER_PROTECTIVE_EXIT' if set(sellkinds)=={'OTHER_PROTECTIVE'} else 'MIXED/PARTIAL'
 unique_runs=sorted(set(o['run_id'] for o in bs));man=readjson(runs[bs[0]['run_id']]['notes_json']).get('run_manifest',{});git=man.get('git_revision') or man.get('runtime_git_revision') or man.get('git_commit')
 if isinstance(git,dict):git=git.get('revision')
 telemetry='v2 stop timing' if slug in latestpos else 'v1 stop timing' if int(slug.rsplit('-',1)[1])>=1791585900 else 'pre-v2 sparse'
 strict=telemetry in ['v1 stop timing','v2 stop timing'];broad=strict or first>=datetime.fromisoformat('2026-09-29T21:46:33+08:00').timestamp()
 # Timestamp is a broad boundary only; exact policy metadata overrides attribution of historical exits.
 labels=[x['p'].get('decision_reason') for x in breakers]
 policy='current-like breaker' if strict else 'unknown' if broad else 'breaker version B' if first>=datetime.fromisoformat('2026-09-25T22:08:03+08:00').timestamp() else 'breaker version A' if breakers else 'pre-breaker / legacy stop'
 policy_evidence='verified run code/STOP_TIMING' if strict else 'commit-time inference only' if broad else 'historical submit has no git manifest; timestamp boundary'
 epoch=int(slug.rsplit('-',1)[1]);day=datetime.fromtimestamp(first,TZ).date().isoformat();utc=datetime.fromtimestamp(first,ZoneInfo('UTC')).date().isoformat()
 st=[x for x in strategy if x['p'].get('slug')==slug];settle=[x['p'] for x in st if x['event_type']=='MARKET_SETTLEMENT'];final=settle[-1] if settle else {};margin=final.get('settlement_reference_margin_bps');entry_m=mark.get(bs[0]['client_order_id'],{})
 cycle=[x['p'] for x in st if x['event_type']=='MARKET_CYCLE_PNL'];journal=sum(x.get('cycle_combined_pnl_usdc') or 0 for x in cycle) if cycle else None
 delta=actual-hold;bp=bool(breakers)
 row={'market_slug':slug,'market_epoch':epoch,'Taipei_day':day,'UTC_day':utc,'run_id':unique_runs,'git_commit':git,'strategy_cohort':'OCT_LATEST_V2' if telemetry=='v2 stop timing' else 'OCT_EARLY_V1' if strict else 'SEPTEMBER','sizing_cohort':'share_v1' if strict else 'legacy','breaker_policy_cohort':policy,'policy_boundary_evidence':policy_evidence,'telemetry_cohort':telemetry,'weekend_flag':datetime.fromtimestamp(first,TZ).weekday()>=5,'held_side':p['side'],'entry_time':first,'entry_qty_gross':rawqty,'entry_share_fee':sharefee,'ENTRY_QTY':qty,'ENTRY_VWAP':cost/rawqty,'ENTRY_NOTIONAL':cost,'entry_effective_cost_per_net_share':cost/qty,'ACTUAL_SELL_QTY':sold,'ACTUAL_SELL_PROCEEDS':proceeds,'ACTUAL_FEES':entryfee+sellfee,'entry_fee_usdc':entryfee,'sell_fee_usdc':sellfee,'RESIDUAL_QTY':residual,'OFFICIAL_OUTCOME':cache[slug]['official_outcome'],'HELD_SIDE_OFFICIAL_WIN':'YES' if winner else 'NO','OFFICIAL_PAYOUT':residual*pay,'ACTUAL_REALIZED_PNL':proceeds-cost*(sold/qty)-entryfee*(sold/qty)-sellfee,'ACTUAL_TOTAL_PNL':actual,'PNL_PER_SHARE':actual/qty,'PNL_PER_INITIAL_NOTIONAL':actual/cost,'HOLD_TO_OFFICIAL_PNL':hold,'hold_per_share':hold/qty,'hold_per_notional':hold/cost,'exit_type':exit_type,'TP_status':'MAKER_TP' if 'MAKER_TP' in sellkinds else 'NO_TP','breaker_exit':bp,'BREAKER_DELTA_VS_HOLD':delta if bp else None,'breaker_delta_per_share':delta/qty if bp else None,'breaker_delta_per_notional':delta/cost if bp else None,'exit_vwap':proceeds/sold if sold else None,'exit_qty_mix':sellkinds,'breaker_label':labels,'current_like_strict':strict,'current_like_broad':broad,'journal_cycle_pnl':journal,'journal_minus_canonical':journal-actual if journal is not None else None,'final_canonical_twap_margin_bps':margin,'official_margin_bps':None,'final_margin_source':'RUNTIME_CANONICAL_TWAP_NOT_OFFICIAL' if margin is not None else 'UNKNOWN','near_tie_bin': '<=1' if margin is not None and abs(margin)<=1 else '1-3' if margin is not None and abs(margin)<=3 else '3-5' if margin is not None and abs(margin)<=5 else '>5' if margin is not None else 'UNKNOWN','class':'FALSE_POSITIVE_BREAKER' if bp and winner else 'TRUE_PROTECTIVE_EXIT' if bp else 'NON_BREAKER_WIN' if winner else 'MISSED_LOSS_NO_BREAKER'}
 allp.append(row)
 if not bp:continue
 successful=sorted(breakers,key=lambda x:x['ts'])[0]
 filltime=min(ts(o['ts']) for o in ss if submit.get(o['client_order_id'],{}).get('p',{}).get('decision_reason')=='absolute_max_loss_breaker')
 attempts=[o for o in orders if o['event_type']=='ORDER_TAKER_EXIT_SUBMIT' and o['p'].get('decision_reason')=='absolute_max_loss_breaker' and first<=ts(o['ts'])<=filltime and (o.get('instrument_id') or o['p'].get('instrument_id','')).split('-')[0]==p['condition']]
 b=min(attempts,key=lambda x:x['ts']) if attempts else successful;btime=ts(b['ts']);elig=next((x['p'].get('obs_wall_ts') for x in st if x['event_type']=='STOP_TIMING_BREAKER_FIRST_ELIGIBLE'),None)
 netobs=[(ts(x['ts']),x['p'].get('net_if_exit'),x['p'].get('best_bid')) for x in st if x['event_type']=='EXIT_POLICY_DECISION' and x['p'].get('net_if_exit') is not None and first<=ts(x['ts'])<=filltime]
 netobs += [(x['p']['obs_wall_ts'],x['p']['net_if_exit'],x['p']['best_bid']) for x in st if x['p'].get('obs_wall_ts') and x['p'].get('net_if_exit') is not None];netobs.sort()
 times={str(n):next((t for t,v,bid in netobs if v<=-n),None) for n in [1,1.5,2,2.5]}
 h=hist.get(slug,{})
 if not times['2'] and h.get('first_minus2_rel_s'):times['2']=first+float(h['first_minus2_rel_s'])
 netminus=next((v for t,v,bid in netobs if times['2'] and t==times['2']),None);en=next((x['p'].get('net_if_exit') for x in st if x['event_type']=='STOP_TIMING_BREAKER_FIRST_ELIGIBLE'),None)
 timing.append({'market_slug':slug,'Taipei_day':day,'official_win':winner,'timing_quality':'V1/V2_COMPONENT_EVENT' if elig else 'SPARSE_21S;ELIGIBILITY_UNKNOWN','T_FIRST_LOSS_SIGNAL':next((t for t,v,bid in netobs if v<0),None),'T_MINUS1':times['1'],'T_MINUS1_5':times['1.5'],'T_MINUS2':times['2'],'T_MINUS2_5':times['2.5'],'T_BREAKER_ELIGIBLE':elig,'T_SUBMIT':btime,'T_FILL':filltime,'MINUS2_TO_ELIGIBLE':elig-times['2'] if elig and times['2'] else None,'MINUS2_TO_SUBMIT':btime-times['2'] if times['2'] else None,'ELIGIBLE_TO_FILL':filltime-elig if elig else None,'SUBMIT_TO_FILL':filltime-btime,'net_at_minus2':netminus,'net_at_eligibility':en,'realized_net_at_exit':row['ACTUAL_REALIZED_PNL'],'deterioration_minus2_to_eligibility':en-netminus if en is not None and netminus is not None else None,'loss_source':'CLASSIFICATION_ERROR' if winner else 'POLICY_CONFIRMATION_DELAY' if elig and times['2'] and elig-times['2']>3 else 'UNKNOWN','submit_est_net':b['p'].get('est_net_if_exit')})
 features.append({'market_slug':slug,'official_win':winner,'entry_price':row['ENTRY_VWAP'],'entry_tte':entry_m.get('entry_time_left_sec'),'exit_tte':b['p'].get('time_left_sec'),'final_canonical_margin_bps':margin,'near_tie_bin':row['near_tie_bin'],'actual_breaker_pnl':actual,'hold_pnl':hold,'delta':delta,'telemetry_cohort':telemetry,'token_dd_015_rel_sec':h.get('TOKEN_dd_ge_0.15_rel_s'),'binance_3bps_rel_sec':h.get('BINANCE_adv_ge_3bps_rel_s'),'chainlink_cross_rel_sec':h.get('CROSS_chainlink_spot_strike_rel_s'),'twap_cross_rel_sec':h.get('CROSS_twap_strike_rel_s'),'sigma':None,'z':None,'trajectory_v2':latestpos.get(slug,{}).get('canonical_actual_exposure'),'max_dd':latestpos.get(slug,{}).get('max_dd_before_exit'),'depth':'UNKNOWN_HISTORICAL' if not strict else 'SPARSE_TRANSITION_ONLY','near_exit_strike_distance_bps':next((x['p'].get('signed_distance_bps') for x in st if x['event_type']=='STOP_TIMING_BREAKER_FIRST_ELIGIBLE'),None)})
def metrics(pp):
 bb=[x for x in pp if x['breaker_exit']];vv=[x['BREAKER_DELTA_VS_HOLD'] for x in bb];save=sum(max(0,x['BREAKER_DELTA_VS_HOLD']) for x in bb if x['HELD_SIDE_OFFICIAL_WIN']=='NO');whip=-sum(min(0,x['BREAKER_DELTA_VS_HOLD']) for x in bb if x['HELD_SIDE_OFFICIAL_WIN']=='YES');net=sum(vv);actual=sum(x['ACTUAL_TOTAL_PNL'] for x in pp)
 return {'positions':len(pp),'days':len(set(x['Taipei_day'] for x in pp)),'breaker_days':len(set(x['Taipei_day'] for x in bb)),'breaker_exits':len(bb),'official_win_breaker_exits':sum(x['HELD_SIDE_OFFICIAL_WIN']=='YES' for x in bb),'official_loss_breaker_exits':sum(x['HELD_SIDE_OFFICIAL_WIN']=='NO' for x in bb),'saved_loss':save,'whipsaw_cost':whip,'net_breaker_value':net,'net_per_initial_notional':net/sum(x['ENTRY_NOTIONAL'] for x in bb) if bb else None,'mean_delta_per_share':statistics.mean(x['breaker_delta_per_share'] for x in bb) if bb else None,'median_delta_per_share':statistics.median(x['breaker_delta_per_share'] for x in bb) if bb else None,'mean_delta_per_notional':statistics.mean(x['breaker_delta_per_notional'] for x in bb) if bb else None,'median_delta':statistics.median(vv) if vv else None,'mean_delta':statistics.mean(vv) if vv else None,'actual_total_pnl':actual,'no_breaker_pnl':actual-net,'no_breaker_delta':-net,'point_estimate_tendency':'MODIFY' if net<0 else 'POSITIVE_NEEDS_KEEP_GATES' if net>0 else 'INCONCLUSIVE'}
cohorts={'ALL':allp,'SEPTEMBER':[x for x in allp if x['Taipei_day'].startswith('2026-09')],'OCTOBER':[x for x in allp if x['Taipei_day'].startswith('2026-10')],'CURRENT_LIKE_STRICT':[x for x in allp if x['current_like_strict']],'CURRENT_LIKE_BROAD':[x for x in allp if x['current_like_broad']],'SCHEMA_V2':[x for x in allp if x['telemetry_cohort']=='v2 stop timing']}
for name in ['breaker version A','breaker version B','unknown']:cohorts[name]=[x for x in allp if x['breaker_policy_cohort']==name]
cs=[{'cohort':name,**metrics(pp)} for name,pp in cohorts.items()];days=sorted(set(x['Taipei_day'] for x in allp));daily=[]
for day in days:daily.append({'Taipei_day':day,**metrics([x for x in allp if x['Taipei_day']==day]),'leave_one_day_out_net':metrics([x for x in allp if x['Taipei_day']!=day])['net_breaker_value']})
bb=sorted([x for x in allp if x['breaker_exit']],key=lambda x:x['BREAKER_DELTA_VS_HOLD'])
csvwrite('all_live_positions.csv',allp);csvwrite('breaker_positions.csv',bb);csvwrite('cohort_sensitivity.csv',cs);csvwrite('daily_breaker_value.csv',daily);csvwrite('timing_decomposition.csv',timing);csvwrite('feature_context_breakers.csv',features);csvwrite('excluded_and_unpaired.csv',excluded);csvwrite('identity_corrections.csv',identity_corrections)
bootstrap=None
breakerdays=[x['net_breaker_value'] for x in daily if x['breaker_exits']]
if len(breakerdays)>=5:
 rand=random.Random(20261010);samples=sorted(sum(rand.choices(breakerdays,k=len(breakerdays))) for _ in range(10000));bootstrap={'n_independent_breaker_days':len(breakerdays),'unit':'Taipei breaker-event day, conditional on retained activity','replicates':10000,'seed':20261010,'ci95_total':[samples[250],samples[9749]]}
inventory=[]
hot={x['id'] for x in orders if x['event_type']=='ORDER_FILLED'}
for path in [ROOT/'backups/trade_journal.db',ROOT/'data/analysis_snapshots/freshness_audit_20261006_231849_+0800/trade_journal_snapshot.db']:
 c=sqlite3.connect(f'file:{path}?mode=ro',uri=True);ids={x[0] for x in c.execute("select id from order_events where event_type='ORDER_FILLED'")};inventory.append({'source':str(path),'fill_rows':len(ids),'additional_fill_ids':sorted(ids-hot)});c.close()
result={'metrics':metrics(allp),'cohort_sensitivity':cs,'day_blocked_bootstrap':bootstrap,'data_inventory':inventory,'unpaired':excluded,'official_cache_sources':[str(x) for x in cache_paths],'identity_correction_count':len(identity_corrections),'fee_policy':'buy share fee deducted from acquired qty; buy USDC fee and actual SELL USDC fees charged once; HOLD excludes hypothetical SELL fees','policy_boundary':'strict=Oct STOP_TIMING runs verified current predicate; broad includes fills after f5a0f66 commit time (inferred); historical A/B by commit-time, not guaranteed deployed','unit':'market + held side actual LIVE BUY exposure; archive/backup not counted twice','rows':allp,'timing':timing}
(OUT/'summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False));print(json.dumps({'metrics':result['metrics'],'cohorts':cs,'excluded':excluded,'bootstrap':bootstrap},indent=2))
