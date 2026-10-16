import sys,json,csv,sqlite3,statistics,hashlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal as D
sys.path.insert(0,'/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main')
from research.early_warning_timeline import load_positions,normalize_row,position_timeline,dec,_adverse
ROOT=Path('/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main')
OUT=ROOT/'reports/post_live_deterioration_audit_20261010'
RUN='run_1791621743_36d99e96'; TZ=ZoneInfo('Asia/Taipei')
j=sqlite3.connect(f'file:{ROOT}/logs/trade_journal.db?mode=ro',uri=True)
r=sqlite3.connect(f'file:{ROOT}/data/research/twap_forward_shadow.db?mode=ro',uri=True)
official=json.loads((OUT/'official_resolution_verification.json').read_text())
assert all(x['closed'] and x['uma_resolution_status']=='resolved' for x in official['markets'])
off={x['slug']:x['official_outcome'] for x in official['markets']}
S=[dict(json.loads(raw),journal_event=e,journal_ts=ts) for ts,e,raw in j.execute('select ts,event_type,payload_json from strategy_events where run_id=? order by id',(RUN,))]
O=[dict(json.loads(raw or '{}'),journal_event=e,journal_ts=ts,order_side=side,order_price=price,order_qty=qty,coid=coid,commission=fee) for ts,e,side,price,qty,coid,fee,raw in j.execute('select ts,event_type,side,price,qty,client_order_id,commission_usdc,payload_json from order_events where run_id=? order by id',(RUN,))]
R=[json.loads(raw) for raw, in r.execute('select payload_json from lead_lag_decisions where run_id=? order by decision_epoch_ns',(RUN,))]
def local(t):return datetime.fromtimestamp(t,TZ).isoformat(timespec='milliseconds') if t is not None else None
def fee(q,p):
 p=max(.01,min(.99,p));return q*p*.072*p*(1-p)
def engine_net(q,entry,bid):
 p=bid*.998;return q*(p-entry)-fee(q,p)
def csvout(name,rows):
 keys=list(dict.fromkeys(k for x in rows for k in x))
 with (OUT/name).open('w',newline='') as f:
  w=csv.DictWriter(f,keys);w.writeheader()
  for x in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in x.items()})
def nearest_before(rows,t,maxgap=3):
 rr=[x for x in rows if x['t']<=t+1e-6 and t-x['t']<=maxgap];return rr[-1] if rr else None
timeline=[]; episodes=[]; leads=[]; execution=[]; cfs=[]; events=[]; results=[]; depths_all=[]
for p in load_positions(j,RUN,False):
 slug=p['slug']; label={'1791621900':'16:45','1791623700':'17:15','1791624600':'17:30'}[slug.rsplit('-',1)[1]]
 ss=[x for x in S if x.get('slug')==slug]; opened=next(x for x in ss if x['journal_event']=='STOP_TIMING_POSITION_OPENED')
 fills=[x for x in O if x['journal_event']=='ORDER_FILLED' and x.get('slug')==slug]
 sell=[x for x in fills if x['order_side']=='SELL'];exit_ts=min(datetime.fromisoformat(x['journal_ts']).timestamp() for x in sell)
 last_fill=max(datetime.fromisoformat(x['journal_ts']).timestamp() for x in sell)
 qty=opened['entry_qty'];entry=opened['entry_price'];payout=int(off[slug]==p['held_side'])
 buyqty=sum(x['order_qty'] for x in fills if x['order_side']=='BUY');sold=sum(x['order_qty'] for x in sell)
 residual=buyqty-sold;fillnet=sum(x['order_price']*x['order_qty']*(1 if x['order_side']=='SELL' else -1)-(x.get('effective_fee_usdc') or 0) for x in fills)
 actual=fillnet+residual*payout;hold=qty*(payout-entry)-(opened.get('entry_fee_remaining') or 0)
 snap=[x for x in R if x.get('event_type')=='PREDICTION_RESEARCH_SNAPSHOT' and x.get('market_slug')==slug and p['entry_ts']<=x['snapshot_ts']<=int(slug.rsplit('-',1)[1])+900]
 a=position_timeline(snap,held_side=p['held_side'],entry_ts=p['entry_ts'],entry_bid=dec(p['entry_bid']),entry_binance=dec(p['entry_binance']),strike=dec(p['strike']),events=p['events'],end_ts=exit_ts)
 post=position_timeline(snap,held_side=p['held_side'],entry_ts=p['entry_ts'],entry_bid=dec(p['entry_bid']),entry_binance=dec(p['entry_binance']),strike=dec(p['strike']),events=p['events'],end_ts=int(slug.rsplit('-',1)[1])+900)
 stop=[x for x in ss if x.get('obs_wall_ts') is not None]; stop.sort(key=lambda x:x['obs_wall_ts'])
 ds=[]
 for x in stop:
  age=x.get('exit_l2_age_sec');fresh=age is not None and -1<=age<=2
  ds.append({'t':x['obs_wall_ts'],'source':'STOP_TIMING','bid':x.get('best_bid'),'sellable_qty':x.get('sellable_qty'),'top_qty':x.get('exit_top_bid_size'),'covers':x.get('exit_depth_covers_qty'),'vwap':x.get('exit_vwap_for_qty'),'fresh':fresh,'original_fresh':x.get('exit_l2_fresh'),'age':max(0,age) if fresh else age,'raw_age':age,'available_qty':x.get('exit_depth_total_size'),'freshness_correction':bool(fresh and not x.get('exit_l2_fresh'))})
 for x in R:
  if x.get('instrument_id')!=p['instrument_id']:continue
  if x.get('event_type')=='SHADOW_BBO_SNAPSHOT':
   age=x.get('quote_age_sec'); top=x.get('best_bid_size'); ds.append({'t':x['event_ts'],'source':'FRESH_QUOTE_SIZE_BBO','bid':x['best_bid'],'sellable_qty':None,'top_qty':top,'covers':top is not None and top>=qty,'vwap':x['best_bid'] if top is not None and top>=qty else None,'fresh':age is not None and age<=2,'original_fresh':None,'age':age,'raw_age':age,'available_qty':top,'freshness_correction':False})
  elif x.get('event_type')=='DECISION_POINT_L2':
   bids=x.get('l2',{}).get('bids') or []; need=qty;value=0
   for px,q in bids:
    take=min(q,need);value+=take*px;need-=take
   ds.append({'t':x['captured_ts'],'source':'DECISION_L2_AGE_UNKNOWN','bid':bids[0][0] if bids else None,'sellable_qty':None,'top_qty':bids[0][1] if bids else None,'covers':need<=1e-9,'vwap':value/qty if need<=1e-9 else None,'fresh':False,'original_fresh':None,'age':None,'raw_age':None,'available_qty':sum(x[1] for x in bids),'freshness_correction':False})
 # Fresh capture clock alone does not prove that a cached L2 matches the executable quote.
 for d in ds:
  if d['source']=='STOP_TIMING':
   st=next(x for x in stop if x['obs_wall_ts']==d['t'])
   d['l2_quote_consistent']=d['vwap'] is None or (d['bid'] is not None and d['vwap']<=d['bid']+1e-9)
   d['valid_depth']=d['fresh'] and d['l2_quote_consistent']
  else:d['l2_quote_consistent']=None;d['valid_depth']=d['fresh']
 ds.sort(key=lambda x:x['t'])
 def context(t):
  raw=nearest_before([dict(x,t=x['snapshot_ts']) for x in snap],t)
  st=nearest_before([dict(x,t=x['obs_wall_ts']) for x in stop],t)
  d=nearest_before(ds,t)
  if raw is None:return {'ts':t,'taipei':local(t),'context_status':'UNOBSERVED'}
  n=normalize_row(raw,p['held_side']); bid=float(n['bid']) if n['bid'] is not None else None
  def state(v):return 'UNKNOWN' if v is None else 'ADVERSE' if _adverse(p['held_side'],D(str(v)),D(str(p['strike']))) else 'FAVORABLE'
  return {'ts':t,'taipei':local(t),'snapshot_ts':raw['snapshot_ts'],'context_age_sec':t-raw['snapshot_ts'],'tte':int(slug.rsplit('-',1)[1])+900-t,'bid':bid,'dd':float(D(str(p['entry_bid']))-D(str(bid))) if bid is not None else None,'bid_state':n['bid_state'],'quote_age_sec':raw.get('held_side_quote_age_sec',raw.get('market_quote_'+p['held_side'].lower()+'_receive_age_sec')),'binance_spot':float(n['binance']) if n['binance'] else None,'binance_age_sec':raw.get('btc_age_sec'),'binance_move_bps':(float(n['binance'])-p['entry_binance'])/p['entry_binance']*10000*(1 if p['held_side']=='DOWN' else -1) if n['binance'] else None,'chainlink_spot':float(n['chainlink']) if n['chainlink'] else None,'chainlink_age_sec':raw.get('chainlink_spot_age_sec'),'chainlink_state':state(n['chainlink']),'chainlink_minus_strike':float(n['chainlink'])-p['strike'] if n['chainlink'] else None,'twap':float(n['twap']) if n['twap'] else None,'twap_age_sec':raw.get('twap_age_sec'),'twap_fresh':raw.get('twap_fresh'),'twap_state':state(n['twap']),'twap_minus_strike':float(n['twap'])-p['strike'] if n['twap'] else None,'sigma':raw.get('required_move_sigma'),'z':raw.get('required_move_z_diffusion'),'required_move_mode':raw.get('required_move_mode'),'net_if_exit_reconstructed':engine_net(qty,entry,bid) if bid is not None else None,'net_if_exit_recorded':st.get('net_if_exit') if st else None,'net_recorded_ts':st.get('obs_wall_ts') if st else None,'sellable_qty':d.get('sellable_qty') if d else None,'exit_depth_covers_qty':d.get('covers') if d else None,'exit_vwap_for_qty':d.get('vwap') if d else None,'exit_l2_fresh':d.get('fresh') if d and d['source']=='STOP_TIMING' else None,'depth_source':d.get('source') if d else None,'depth_ts':d.get('t') if d else None,'depth_age_from_capture_sec':t-d['t'] if d else None,'depth_freshness_correction':d.get('freshness_correction') if d else None}
 # Sparse actual net event evidence merged with modelled snapshot net; retain clocks separately.
 observed=[dict(t=x['snapshot_ts'],net=engine_net(qty,entry,float(normalize_row(x,p['held_side'])['bid'])),source='SNAPSHOT_ENGINE_FORMULA') for x in snap if x['snapshot_ts']<=exit_ts and normalize_row(x,p['held_side'])['bid'] is not None]
 observed += [dict(t=x['obs_wall_ts'],net=x['net_if_exit'],source='RECORDED_ENGINE') for x in stop if x.get('net_if_exit') is not None and x['obs_wall_ts']<=exit_ts]
 observed.sort(key=lambda x:x['t'])
 ev={k:v['ts'] for k,v in a['timeline'].items()};ev['EXIT_FILL']=exit_ts
 net_thresholds={}
 for threshold in [1,1.5,2,2.5,3]:
  hit=next((x for x in observed if x['net']<=-threshold),None);name=f'NET_LE_{threshold:.2f}';ev[name]=hit['t'] if hit else None;net_thresholds[name]=hit
 # Favorable recross after first adverse observation; no interpolation across unknown observations.
 for field,name in [('chainlink','CHAINLINK'),('twap','TWAP')]:
  adverse_seen=False;recross=None;prev=None;prev_t=None
  for raw in snap:
   if raw['snapshot_ts']>exit_ts:break
   n=normalize_row(raw,p['held_side']);v=n[field]
   if v is None:prev=None;continue
   state=_adverse(p['held_side'],v,D(str(p['strike'])))
   if state:adverse_seen=True
   if adverse_seen and prev is True and not state:recross=n['t'];break
   prev=state;prev_t=n['t']
  ev[f'T_{name}_FAVORABLE_RECROSS']=recross
 # Full path CSV needed for the requested detailed case; post-exit rows explicitly not observations of a full position.
 obsrows=[]
 for raw in snap:
  t=raw['snapshot_ts'];c=context(t);c.update(position=label,slug=slug,scope='ACTUAL_EXPOSURE' if t<=exit_ts else 'POST_EXIT_HYPOTHETICAL_FULL_QTY',qty_for_formula=qty,actual_remaining_qty=next((x.get('position_qty_after') for x in reversed(sell) if datetime.fromisoformat(x['journal_ts']).timestamp()<=t),qty),warmup=label=='16:45',strike=p['strike'])
  c['breaker_state']='ELIGIBLE' if ev.get('T_BREAKER_ELIGIBLE') and t>=ev['T_BREAKER_ELIGIBLE'] else 'NOT_YET_ELIGIBLE' if label=='17:15' else 'NOT_TRIGGERED'
  for x,s in a['token'].items():
   for var in ['primary','hysteresis']:
    c[f'episode_{var}_{x}']=next((i+1 for i,ep in enumerate(s[var]['episodes']) if ep['start_ts']<=t and (ep['end_ts'] is None or t<ep['end_ts'])),0) if t<=exit_ts else 'POST_EXIT'
  timeline.append(c);obsrows.append(c)
 for name,t in ev.items():
  c=context(t) if t else {}
  exact=next((x for x in stop if t is not None and abs(x['obs_wall_ts']-t)<1e-6),None)
  if exact:
   c.update(context_source='EXACT_STOP_TIMING',bid=exact['best_bid'],dd=float(D(str(p['entry_bid']))-D(str(exact['best_bid']))),net_if_exit_recorded=exact['net_if_exit'],net_recorded_ts=t,net_if_exit_reconstructed=engine_net(qty,entry,exact['best_bid']),binance_spot=exact.get('binance_spot'),binance_move_bps=exact.get('binance_adverse_move_bps'),chainlink_spot=exact.get('chainlink_spot'),chainlink_state=exact.get('chainlink_cross_state'),twap=exact.get('settlement_reference_twap'),twap_state=exact.get('cross_state'),sigma=exact.get('required_move_sigma_legacy'),z=exact.get('required_move_z_diffusion'))
  events.append({'position':label,'event':name,'status':'TRIGGERED' if t is not None else 'NOT_TRIGGERED',**c})
 def shadow(t,maxwait=5):
  if t is None:return {'status':'NOT_TRIGGERED','shadow_exit_net':actual,'delta_vs_actual':0,'delta_vs_hold':actual-hold,'basis':'ACTUAL_POLICY_UNCHANGED'}
  if t>=exit_ts:return {'status':'CENSORED_AT_ACTUAL_EXIT','shadow_exit_net':actual,'delta_vs_actual':0,'delta_vs_hold':actual-hold}
  quotes=[x for x in ds if t-1e-6<=x['t']<=min(t+maxwait,exit_ts) and x['covers'] and x['valid_depth'] and x['vwap'] is not None]
  if not quotes:return {'status':'UNKNOWN','shadow_exit_net':None,'delta_vs_actual':None,'delta_vs_hold':None}
  d=quotes[0];q=d['sellable_qty'] if d['sellable_qty'] is not None else qty*.999
  # Unsold residue receives verified official payout; mark payout contribution separately.
  net=q*d['vwap']-qty*entry-fee(q,d['vwap'])+(qty-q)*payout
  return {'status':'DEPTH_SUPPORTED_ESTIMATE','quote_ts':d['t'],'quote_taipei':local(d['t']),'quote_delay_sec':d['t']-t,'quote_source':d['source'],'exit_vwap_for_qty':d['vwap'],'sell_qty':q,'residue_qty':qty-q,'residue_settlement_payout':(qty-q)*payout,'shadow_exit_net':net,'delta_vs_actual':net-actual,'delta_vs_hold':net-hold,'freshness_correction':d['freshness_correction']}
 for x,s in a['token'].items():
  prim=s['primary'];hyst=s['hysteresis']
  for var in ['primary','hysteresis']:
   b=s[var];first=b['episodes'][0] if b['episodes'] else None;second=b['episodes'][1] if len(b['episodes'])>1 else None
   start=b['first_trigger_ts'];rec=b['first_recovery_ts'];duration=b['first_episode_duration_sec'];after=[z for z in obsrows if start is not None and start<=z['ts']<=exit_ts and z['bid'] is not None]
   pre=[z['bid'] for z in obsrows if start is not None and max(p['entry_ts'],start-30)<=z['ts']<start and z['bid'] is not None];high=max(pre) if pre else None
   summary={'position':label,'slug':slug,'threshold':x,'variant':var.upper(),'primary_episodes':prim['episode_count'],'hysteresis_episodes':hyst['episode_count'],'flap_count':s['flap_count'],'first_dd_ts':start,'first_dd_taipei':local(start),'first_recovery_ts':rec,'first_recovery_taipei':local(rec),'first_recovery_confirmed_ts':first.get('recovery_confirmed_ts') if first else None,'first_episode_duration_sec':duration,'first_episode_duration_min_sec':first.get('duration_min_sec') if first else None,'first_episode_duration_max_sec':first.get('duration_max_sec') if first else None,'second_dd_ts':b['second_trigger_ts'],'second_dd_taipei':local(b['second_trigger_ts']),'time_recovery_to_second_dd_sec':b['time_recovery_to_second_trigger_sec'],'max_dd_first_episode':first.get('max_dd') if first else None,'max_dd_second_episode':second.get('max_dd') if second else None,'recovered_to_entry_bid':any(z['bid']>=p['entry_bid'] for z in after) if start else None,'pre_drop_local_high_30s':high,'recovered_to_pre_drop_local_high':any(z['bid']>=high for z in after) if high else None,'recovered_but_second_dd':bool(rec and b['second_trigger_ts']),'no_recovery_before_exit':bool(start and rec is None),'censored':bool(first and (first['spans_unobserved'] or first['censored_at_end'])),'observation_gaps':b['coverage']['gaps']}
   # Hysteresis windows use the confirmation time, not the backdated recovery start.
   recovery_known=first.get('recovery_confirmed_ts') if var=='hysteresis' and first else rec
   for sec in [5,15,30,60,120]:summary[f'recovered_within_{sec}s']=recovery_known-start<=sec if recovery_known and start else False if start else None
   for prefix,t in [('first_dd',start),('first_recovery',rec),('second_dd',b['second_trigger_ts'])]:
    if t is not None:summary.update({prefix+'_'+k:v for k,v in context(t).items() if k not in ['ts','taipei']})
   episodes.append(summary)
   for k,ep in enumerate(b['episodes']):events.append({'position':label,'event':f'{var.upper()}_DD_{x}_EPISODE_{k+1}',**ep,**context(ep['start_ts'])})
  for leg,t in [('FIRST',prim['first_trigger_ts']),('SECOND',prim['second_trigger_ts']),('FIRST_HYSTERESIS',hyst['first_trigger_ts']),('SECOND_HYSTERESIS',hyst['second_trigger_ts'])]:
   c=context(t) if t else {};d=nearest_before(ds,t,1) if t else None
   exact='YES' if d and d['valid_depth'] and d['covers'] and d['vwap'] is not None and c.get('bid') is not None and abs(d['bid']-c['bid'])<1e-9 else 'NO' if d and d['valid_depth'] and d['source']=='STOP_TIMING' and d['covers'] is False else 'UNKNOWN'
   execution.append({'position':label,'threshold':x,'leg':leg,'signal_ts':t,'signal_taipei':local(t),'executable_at_signal':exact,'at_signal_evidence_source':d.get('source') if d else None,'at_signal_evidence_ts':d.get('t') if d else None,'at_signal_evidence_lag_sec':t-d['t'] if d else None,'l2_quote_consistent':d.get('l2_quote_consistent') if d else None,**c,**{'nearest_after_'+k:v for k,v in shadow(t).items()}})
  for target in ['NET_LE_1.00','NET_LE_1.50','NET_LE_2.00','NET_LE_2.50','NET_LE_3.00','T_MINUS2','T_BREAKER_ELIGIBLE','T_PROTECTIVE_EXIT_SUBMIT','EXIT_FILL','T_CHAINLINK_SPOT_CROSS','T_TWAP_CROSS']:
   t=prim['first_trigger_ts'];tt=ev.get(target);delta=tt-t if t is not None and tt is not None else None
   leads.append({'position':label,'signal':f'DD_{x}_FIRST','target':target,'signal_ts':t,'target_ts':tt,'lead_sec':delta,'grid_label':'SIMULTANEOUS_APPROX' if delta is not None and abs(delta)<2 else 'SIGNAL_LEADS' if delta is not None and delta>0 else 'SIGNAL_LAGS' if delta is not None else 'NOT_COMPARABLE','grid_tolerance_sec':2})
 for bn in [1,2,3,5]:
  for x in ['0.05','0.10','0.15','0.20']:
   t=ev.get(f'T_BINANCE_{bn}BPS');tt=a['token'][x]['primary']['first_trigger_ts'];delta=tt-t if t is not None and tt is not None else None
   leads.append({'position':label,'signal':f'BINANCE_{bn}BPS','target':f'DD_{x}_FIRST','signal_ts':t,'target_ts':tt,'lead_sec':delta,'grid_label':'SIMULTANEOUS_APPROX' if delta is not None and abs(delta)<2 else 'SIGNAL_LEADS' if delta is not None and delta>0 else 'SIGNAL_LAGS' if delta is not None else 'NOT_COMPARABLE','grid_tolerance_sec':2})
 for n,x,var,leg in [('CF1','0.10','primary','first_trigger_ts'),('CF2','0.15','primary','first_trigger_ts'),('CF3','0.15','hysteresis','first_trigger_ts'),('CF4','0.10','primary','second_trigger_ts'),('CF5','0.15','primary','second_trigger_ts'),('CF6','0.15','primary','second_trigger_ts')]:
  t=a['token'][x][var][leg]
  if n=='CF6' and t is not None:
   value=context(t).get('binance_move_bps')
   if value is None:cfs.append({'position':label,'counterfactual':n,'signal_ts':t,'status':'UNKNOWN_BINANCE'});continue
   if value<=0:t=None
  cfs.append({'position':label,'counterfactual':n,'signal_ts':t,'signal_taipei':local(t),**shadow(t)})
 cfs.extend([{'position':label,'counterfactual':'CF7','status':'ACTUAL_BREAKER' if p['exit_fill_kind']=='PROTECTIVE_TAKER' else 'NO_BREAKER_ACTUAL_TP','shadow_exit_net':actual,'delta_vs_actual':0,'delta_vs_hold':actual-hold},{'position':label,'counterfactual':'CF8','status':'OFFICIAL_SETTLEMENT','shadow_exit_net':hold,'delta_vs_actual':hold-actual,'delta_vs_hold':0}])
 for d in ds:depths_all.append({'position':label,**d,'taipei':local(d['t'])})
 runtime=[x for x in ss if x['journal_event']=='MARKET_SETTLEMENT'];runtime_last=runtime[-1] if runtime else {}
 result={'position':label,'warmup':label=='16:45','slug':slug,'held_side':p['held_side'],'qty':qty,'entry_price':entry,'entry_bid':p['entry_bid'],'entry_ts':p['entry_ts'],'entry_taipei':local(p['entry_ts']),'exit_first_ts':exit_ts,'exit_last_ts':last_fill,'exit_taipei':local(exit_ts),'exit_fill_kind':p['exit_fill_kind'],'sold_qty':sold,'residual_qty':residual,'fill_cashflow_net':fillnet,'actual_realized_sell_net':sum(x.get('realized_net_usdc') or 0 for x in sell),'actual_with_official_residual_payout':actual,'hold_official_pnl':hold,'official_outcome':off[slug],'runtime_outcome_last':runtime_last.get('outcome'),'runtime_payload':runtime_last,'settlement_telemetry_outcome':next(x['settlement_outcome_runtime'] for x in reversed(ss) if x['journal_event']=='STOP_TIMING_POSITION_SETTLEMENT'),'events':ev,'net_thresholds':net_thresholds,'canonical_actual_exposure':a,'canonical_post_exit_hypothetical':post,'max_dd_before_exit':max((x['dd'] for x in obsrows if x['ts']<=exit_ts and x['dd'] is not None),default=None),'min_net_before_exit':min(x['net'] for x in observed),'max_binance_adverse_before_exit':max(x['binance_move_bps'] for x in obsrows if x['ts']<=exit_ts and x['binance_move_bps'] is not None),'book_empty_count_before_exit':sum(x['bid_state']=='BOOK_EMPTY' for x in obsrows if x['ts']<=exit_ts),'gap_count_actual':len(a['token']['0.10']['primary']['coverage']['gaps'])}
 results.append(result)
csvout('position_timeline.csv',timeline);csvout('episode_table.csv',episodes);csvout('lead_time_table.csv',leads);csvout('executability_table.csv',execution);csvout('counterfactuals.csv',cfs);csvout('event_context_table.csv',events);csvout('depth_evidence.csv',depths_all)
csvout('position_summary.csv',[{k:v for k,v in x.items() if not isinstance(v,(dict,list))} for x in results])
summary={'run_id':RUN,'live_head':'7a597551bd596e9e734cdcd8ef14a6b75d91e8c9','analysis_head':'94f1c47','official_verification':official,'positions':results,'method':{'read_only_sqlite':'mode=ro','actual_exposure_censor':'first SELL fill; residual and last fill separately retained','snapshot_grid':'best available, typically 1.1-1.3s; gaps >3s censored, never interpolated','simultaneous_tolerance_sec':2,'depth_signal_max_backward_age_sec':1,'shadow_quote_max_wait_sec':5,'net_formula':'q*(bid*0.998-entry)-fee(q,bid*0.998); reconstructed, not recorded engine evaluation','shadow_net_formula':'sellable_qty*exit_vwap-entry_cost-fee(sellable_qty,exit_vwap)+official residual payout','fee_formula':'frozen runtime 0.072*q*p^2*(1-p), p clamped 0.01..0.99','depth_policy':'fresh STOP_TIMING L2 or fresh quote-size top bid covering full entry qty; unknown-source-age DECISION_POINT_L2 retained as unverified only','historical_l2_correction':'-1<=raw age<0 corrected to age=0 FRESH; original preserved','hysteresis':'2 ticks plus >=5 seconds continuous observed recovery; triggering time unchanged','local_high_window_sec':30,'CF6_binance_adverse':'signed adverse move >0 at second primary DD0.15; no new bps threshold; remains adverse from recovery requires all observed fresh points >0, not established if gaps'}}
summary['median_leads']={}
for x in ['0.10','0.15']:
 for target in ['NET_LE_2.00','T_MINUS2','T_BREAKER_ELIGIBLE']:
  vals=[z['lead_sec'] for z in leads if z['signal']==f'DD_{x}_FIRST' and z['target']==target and z['lead_sec'] is not None]
  summary['median_leads'][x+'_'+target]={'median_sec':statistics.median(vals) if vals else None,'n':len(vals),'values':vals}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False,default=str))
print(json.dumps({'positions':[{k:x[k] for k in ['position','qty','actual_with_official_residual_payout','hold_official_pnl','max_dd_before_exit','min_net_before_exit','max_binance_adverse_before_exit','gap_count_actual']} for x in results],'median_leads':summary['median_leads'],'counterfactuals':cfs,'executability':[{k:x.get(k) for k in ['position','threshold','leg','executable_at_signal','nearest_after_status','nearest_after_quote_delay_sec','nearest_after_shadow_exit_net']} for x in execution if x['leg']=='FIRST']},indent=2))
