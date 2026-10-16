"""Derived (never in-place) correction of canonical_events `fast_spot` provenance.

Since 775dbee, settlement diagnostics overwrote the Binance `fast_spot` with the
settlement path spot (Chainlink raw when fresh, else Binance). `spot_minus_twap_bps`
was computed from the original Binance argument before the overwrite, so
    binance_spot_reconstructed = official_current_twap * (1 + spot_minus_twap_bps / 1e4)
Frozen exports are only read; output goes to this directory.
"""
from pathlib import Path
import json,hashlib
import numpy as np
import pandas as pd
O=Path(__file__).resolve().parent;R=O.parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
COLS=['run_id','market_slug','event_type','observed_ts','source_ts','fast_spot','official_current_twap','spot_minus_twap_bps','path_spot','path_spot_source']
def main():
    files=sorted((R/'data/research_export/P_paths').glob('*/canonical_events.parquet'))
    manifest={str(f.relative_to(R)):sha(f) for f in files}
    out=[];per_day=[]
    for f in files:
        e=pd.read_parquet(f).reindex(columns=COLS);e['export_day']=f.parent.name
        e['binance_spot_reconstructed']=e.official_current_twap*(1+e.spot_minus_twap_bps/1e4)
        cl=e.path_spot_source.eq('polymarket_chainlink_spot')&e.path_spot.gt(0)
        e['chainlink_spot']=e.path_spot.where(cl)
        e['binance_minus_chainlink_bps']=(e.binance_spot_reconstructed-e.chainlink_spot)/e.chainlink_spot*1e4
        e['fast_spot_was_overwritten']=e.fast_spot.notna()&e.path_spot.notna()&np.isclose(e.fast_spot,e.path_spot,rtol=0,atol=1e-9)
        out.append(e)
        per_day.append({'export_day':f.parent.name,'rows':len(e),'has_path_spot_column':bool(e.path_spot.notna().any()),
                        'reconstructable':int(e.binance_spot_reconstructed.notna().sum()),'fast_spot_equals_path_spot':int(e.fast_spot_was_overwritten.sum()),
                        'chainlink_rows':int(cl.sum()),'basis_bps_median':e.binance_minus_chainlink_bps.median()})
    d=pd.concat(out,ignore_index=True);d.to_parquet(O/'canonical_events_spot_provenance_corrected.parquet',index=False)
    pd.DataFrame(per_day).to_csv(O/'per_day.csv',index=False)
    # Validation: reconstructed Binance vs P_paths btc_spot (Binance aggTrade research buffer), <=1 s as-of.
    p=pd.concat([pd.read_parquet(f).reindex(columns=['snapshot_ts','market_slug','btc_spot']) for f in sorted((R/'data/research_export/P_paths').glob('*/paths_historical_pre_v2.parquet'))],ignore_index=True)
    p=p[p.btc_spot.gt(0)].copy();p['snapshot_ts']=p.snapshot_ts.astype(float)
    v=d.dropna(subset=['binance_spot_reconstructed','observed_ts']).copy();v['observed_ts']=v.observed_ts.astype(float)
    m=pd.merge_asof(v.sort_values('observed_ts'),p.sort_values('snapshot_ts'),left_on='observed_ts',right_on='snapshot_ts',by='market_slug',direction='nearest',tolerance=1).dropna(subset=['btc_spot'])
    err=(m.binance_spot_reconstructed-m.btc_spot)/m.btc_spot*1e4
    old=(m.fast_spot-m.btc_spot)/m.btc_spot*1e4
    summary={'rows':len(d),'validation_pairs':len(m),'reconstructed_minus_btc_spot_bps':{'median_abs':float(err.abs().median()),'p90_abs':float(err.abs().quantile(.9))},
             'stored_fast_spot_minus_btc_spot_bps':{'median':float(old.median()),'median_abs':float(old.abs().median())},
             'inputs_unchanged':all(sha(R/k)==h for k,h in manifest.items())}
    (O/'input_manifest.json').write_text(json.dumps(manifest,indent=2));(O/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary,indent=2));print(pd.DataFrame(per_day).round(3).to_string(index=False))
if __name__=='__main__':main()
