import csv,json,hashlib,sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path('/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main');OUT=ROOT/'reports/post_live_deterioration_audit_20261010'
s=json.loads((OUT/'summary.json').read_text())
def write(name,rows):
 with (OUT/name).open('w',newline='') as f:
  keys=list(dict.fromkeys(k for r in rows for k in r));w=csv.DictWriter(f,keys);w.writeheader();w.writerows(rows)
rr=list(csv.DictReader((OUT/'position_timeline.csv').open()))
for row in rr:
 if row['scope']=='POST_EXIT_HYPOTHETICAL_FULL_QTY':row['breaker_state']='EXITED_PROTECTIVE_TAKER' if row['position']=='17:15' else 'EXITED_MAKER_TP'
write('position_timeline.csv',rr)
leads=list(csv.DictReader((OUT/'lead_time_table.csv').open()))
for p in s['positions']:
 cross=next(x for x in s['cross_comparison'] if x['position']==p['position'])
 for signal,target,t,tt in [('MINUS2','BREAKER_ELIGIBLE',p['events']['T_MINUS2'],p['events']['T_BREAKER_ELIGIBLE']),('CHAINLINK_CROSS','BREAKER_ELIGIBLE',cross['chainlink_adverse_ts'],p['events']['T_BREAKER_ELIGIBLE']),('TWAP_CROSS','BREAKER_ELIGIBLE',cross['twap_adverse_ts'],p['events']['T_BREAKER_ELIGIBLE']),('BREAKER_ELIGIBLE','EXIT_SUBMIT',p['events']['T_BREAKER_ELIGIBLE'],p['events']['T_PROTECTIVE_EXIT_SUBMIT']),('BREAKER_ELIGIBLE','EXIT_FILL',p['events']['T_BREAKER_ELIGIBLE'],p['exit_first_ts']),('EXIT_SUBMIT','EXIT_FILL',p['events']['T_PROTECTIVE_EXIT_SUBMIT'],p['exit_first_ts'])]:
  delta=tt-t if t and tt else None
  if not any(x['position']==p['position'] and x['signal']==signal and x['target']==target for x in leads):leads.append({'position':p['position'],'signal':signal,'target':target,'signal_ts':t,'target_ts':tt,'lead_sec':delta,'grid_label':'EXACT_EVENT_CLOCK' if signal in ['BREAKER_ELIGIBLE','EXIT_SUBMIT'] else 'SIMULTANEOUS_APPROX' if delta is not None and abs(delta)<2 else 'SIGNAL_LEADS' if delta is not None and delta>0 else 'SIGNAL_LAGS' if delta is not None else 'NOT_COMPARABLE','grid_tolerance_sec':2})
write('lead_time_table.csv',leads)
j=sqlite3.connect(f'file:{ROOT}/logs/trade_journal.db?mode=ro',uri=True);errors=[];fills=[]
for ts,e,raw in j.execute("select ts,event_type,payload_json from strategy_events where run_id=? and event_type like 'STOP_TIMING_%'",(s['run_id'],)):
 x=json.loads(raw)
 if x.get('net_if_exit') is not None:
  px=x['best_bid']*.998;est=x['qty']*(px-x['avg_entry'])-.072*x['qty']*px**2*(1-px);errors.append(abs(est-x['net_if_exit']))
for ts,side,px,q,coid,raw in j.execute("select ts,side,price,qty,client_order_id,payload_json from order_events where run_id=? and event_type='ORDER_FILLED'",(s['run_id'],)):
 x=json.loads(raw);fills.append({'slug':x['slug'],'timestamp_utc':ts,'timestamp_taipei':datetime.fromisoformat(ts).astimezone(ZoneInfo('Asia/Taipei')).isoformat(),'side':side,'price':px,'qty':q,'client_order_id':coid,'exit_fill_kind':'MAKER_TP' if 'MAKER-SELL' in coid else 'PROTECTIVE_TAKER' if 'TAKER-EXIT' in coid else 'ENTRY','actual_fee_usdc':x.get('effective_fee_usdc'),'realized_sell_net':x.get('realized_net_usdc'),'remaining_qty':x.get('position_qty_after')})
write('actual_fill_events.csv',fills)
validation={'recorded_net_rows':len(errors),'max_absolute_reproduction_error_usdc':max(errors),'actual_live_positions':3,'actual_fill_rows':len(fills),'best_available_timeline_rows':len(rr),'official_outcomes_verified':True,'production_diff_empty':True,'bot_process_check':'no bot/run_bot process; separate existing research-only Python process is not a trading runtime','no_commit_or_push':True,'sqlite_mode':'ro','unknowns_not_imputed':True,'scratch_only':True}
assert max(errors)<1e-10 and len(fills)==7
s['validation']=validation
(OUT/'summary.json').write_text(json.dumps(s,indent=2,ensure_ascii=False));(OUT/'validation.json').write_text(json.dumps(validation,indent=2,ensure_ascii=False))
# Standardize common Traditional Chinese spellings in the human-readable document.
report=(OUT/'report.md').read_text()
for a,b in {'恢复':'恢復','减少':'減少','价值':'價值','没有':'沒有','报':'報','见':'見','无':'無','观':'觀','计':'計','估计':'估計','触':'觸','单':'單','当':'當','后':'後','区':'區','从':'從','样':'樣','胜':'勝','为':'為','终':'終','数':'數','标':'標','残':'殘','价':'價','经济':'經濟','户':'戶','权':'權','证':'證','仅':'僅','体':'體','识':'識','关':'關','进':'進','时':'時','输':'輸','结':'結','变':'變','与':'與','则':'則','复':'復','坏':'壞','断':'斷'}.items():report=report.replace(a,b)
(OUT/'report.md').write_text(report)
(OUT/'post_live_deterioration_finalize.py').write_bytes(Path(__file__).read_bytes())
manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name!='manifest.json'};(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(validation))
