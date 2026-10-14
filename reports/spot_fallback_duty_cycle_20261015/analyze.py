"""Step 2: how often does the live reference spot fall back from Chainlink TWAP to Binance?

Read-only over the main-checkout journal (logs/trade_journal.db, last write 2026-10-12;
not the running forward process's DB). Writes only into this directory.
"""
from pathlib import Path
import json,sqlite3
import pandas as pd
O=Path(__file__).resolve().parent;R=O.parents[1]
DB=R/'logs/trade_journal.db'
TWAP='polymarket_chainlink_twap_60s_ws'
def q(con,sql):return pd.read_sql_query(sql,con)
def main():
    con=sqlite3.connect(f'file:{DB}?mode=ro',uri=True)
    runs=q(con,"select run_id,mode,test_mode from strategy_runs")
    def events(t,fields):
        cols=','.join(f"json_extract(payload_json,'$.{f}') as \"{f}\"" for f in fields)
        d=q(con,f"select e.ts,e.run_id,{cols} from strategy_events e where event_type='{t}'").merge(runs,on='run_id',how='left')
        d['day']=pd.to_datetime(d.ts,utc=True,format='ISO8601').dt.tz_convert('Asia/Taipei').dt.date.astype(str);return d
    out={}
    # Direction decisions.
    sd=events('SIDE_DECISION',['selected_spot_source','reference_spot_source','reference_spot_price','binance_spot_price','selected_spot_age_sec'])
    sd['fallback']=sd.selected_spot_source.ne(TWAP)
    out['side_decision']=sd.groupby('mode').agg(n=('fallback','size'),fallback=('fallback','sum')).assign(rate=lambda x:x.fallback/x.n).reset_index().to_dict('records')
    out['side_decision_sources']=sd.selected_spot_source.value_counts(dropna=False).to_dict()
    sd.groupby(['day','mode']).agg(n=('fallback','size'),fallback=('fallback','sum')).assign(rate=lambda x:x.fallback/x.n).to_csv(O/'side_decision_by_day.csv')
    # Entry traces: does any submit use a non-TWAP spot?
    et=events('ENTRY_DECISION_TRACE',['source_event_type','research_snapshot.spot_source'])
    tab=et.groupby(['source_event_type','research_snapshot.spot_source','mode'],dropna=False).size().rename('n').reset_index();tab.to_csv(O/'entry_trace_by_source.csv',index=False)
    pre=et[et.source_event_type.eq('ENTRY_DECISION_PRE_SUBMIT')]
    out['entry_pre_submit']={'n':len(pre),'non_twap':int(pre['research_snapshot.spot_source'].ne(TWAP).sum())}
    # Entry confirmation observations are periodic: use as a time-share proxy for the stop/exit path.
    ec=events('ENTRY_CONFIRMATION_OBSERVATION',['ref_spot_source','ref_spot_age_sec'])
    ec['fallback']=ec.ref_spot_source.ne(TWAP)
    out['entry_confirmation_time_share_proxy']=ec.groupby('mode').agg(n=('fallback','size'),fallback=('fallback','sum')).assign(rate=lambda x:x.fallback/x.n).reset_index().to_dict('records')
    out['entry_confirmation_sources']=ec.ref_spot_source.value_counts(dropna=False).to_dict()
    # Degradation episodes (both events are rate-limited, so counts are lower bounds).
    for t in ['TWAP_REFERENCE_DEGRADED','POLYMARKET_TWAP_SILENT_STALL']:
        d=events(t,['twap_age_sec']);out[t]={'n':len(d),'by_mode':d['mode'].value_counts().to_dict(),'days_with_events':int(d.day.nunique())}
        d.groupby(['day','mode']).size().rename('n').to_csv(O/f'{t.lower()}_by_day.csv')
    # Binance minus TWAP reference, per day: the basis the fallback would inject (TWAP lag included).
    v=sd[sd.reference_spot_source.eq(TWAP)].dropna(subset=['reference_spot_price','binance_spot_price']).copy()
    v['binance_minus_twap_bps']=(v.binance_spot_price.astype(float)-v.reference_spot_price.astype(float))/v.reference_spot_price.astype(float)*1e4
    bd=v.groupby('day').binance_minus_twap_bps.describe(percentiles=[.1,.5,.9]);bd.to_csv(O/'binance_minus_twap_by_day.csv')
    out['binance_minus_twap_bps_overall']={'median':float(v.binance_minus_twap_bps.median()),'p10':float(v.binance_minus_twap_bps.quantile(.1)),'p90':float(v.binance_minus_twap_bps.quantile(.9))}
    out['journal_span']=q(con,'select min(ts) a,max(ts) b from strategy_events').iloc[0].to_dict()
    (O/'summary.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str));print(bd.round(2).to_string())
if __name__=='__main__':main()
