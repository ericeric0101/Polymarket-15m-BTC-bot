import json,csv,sqlite3,statistics,collections,hashlib
from pathlib import Path
ROOT=Path('/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main');O=ROOT/'reports/consolidated_breaker_value_audit'
s=json.loads((O/'summary.json').read_text());rows=s['rows'];m=s['metrics'];bb=[x for x in rows if x['breaker_exit']]
def readcsv(name):return list(csv.DictReader((O/name).open()))
def writecsv(name,rr):
 with (O/name).open('w',newline='') as f:
  keys=list(dict.fromkeys(k for r in rr for k in r));w=csv.DictWriter(f,keys);w.writeheader()
  for r in rr:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in r.items()})
def table(headers,data):return '| '+' | '.join(headers)+' |\n|'+'|'.join(['---']*len(headers))+'|\n'+'\n'.join('| '+' | '.join(str(v) for v in r)+' |' for r in data)+'\n'
j=sqlite3.connect(f'file:{ROOT}/logs/trade_journal.db?mode=ro',uri=True)
ss=[dict(json.loads(raw),event=e,journal_ts=t) for t,e,raw in j.execute("select ts,event_type,payload_json from strategy_events where event_type in ('STOP_TIMING_BREAKER_FIRST_ELIGIBLE','STOP_TIMING_COMPONENT_FIRST_TRUE','STOP_TIMING_POSITION_OPENED')")]
feat=readcsv('feature_context_breakers.csv')
for x in feat:
 q=next((q for q in ss if q.get('slug')==x['market_slug'] and q['event']=='STOP_TIMING_BREAKER_FIRST_ELIGIBLE'),None)
 if q:
  for k,v in {'sigma':q.get('required_move_sigma_legacy'),'z':q.get('required_move_z_diffusion'),'chainlink_at_eligible':q.get('chainlink_cross_state'),'twap_at_eligible':q.get('cross_state'),'binance_adverse_bps_at_eligible':q.get('binance_adverse_move_bps'),'exit_l2_fresh':q.get('exit_l2_fresh'),'exit_depth_covers_qty':q.get('exit_depth_covers_qty'),'exit_vwap_for_qty':q.get('exit_vwap_for_qty'),'sellable_qty':q.get('sellable_qty'),'depth': 'RECORDED_SPARSE_L2_ELIGIBILITY'}.items():x[k]=v
 latest=json.loads(x['trajectory_v2']) if x.get('trajectory_v2') else None
 for dd in ['0.05','0.10','0.15','0.20']:
  if latest:
   block=latest['token'][dd];x['DD_'+dd+'_first']=block['primary']['first_trigger_ts'];x['DD_'+dd+'_recovery']=block['primary']['first_recovery_ts'];x['DD_'+dd+'_second']=block['primary']['second_trigger_ts'];x['DD_'+dd+'_hysteresis_count']=block['hysteresis']['episode_count']
  else:
   x['DD_'+dd+'_first']=None;x['DD_'+dd+'_recovery']=None;x['DD_'+dd+'_second']=None;x['DD_'+dd+'_hysteresis_count']=None
writecsv('feature_context_breakers.csv',feat)
daily=readcsv('daily_breaker_value.csv');lo=[float(x['leave_one_day_out_net']) for x in daily]
counts=collections.Counter(x['HELD_SIDE_OFFICIAL_WIN'] for x in rows)
maximumcost=-min(x['BREAKER_DELTA_VS_HOLD'] for x in bb);maximumsave=max(x['BREAKER_DELTA_VS_HOLD'] for x in bb)
s['required_numbers']={'TOTAL_LIVE_POSITIONS':len(rows),'TOTAL_INDEPENDENT_TAIPEI_DAYS':m['days'],'OFFICIAL_WIN_POSITIONS':counts['YES'],'OFFICIAL_LOSS_POSITIONS':counts['NO'],'BREAKER_EXIT_POSITIONS':len(bb),'BREAKER_OFFICIAL_WINNERS':m['official_win_breaker_exits'],'BREAKER_OFFICIAL_LOSERS':m['official_loss_breaker_exits'],'UNKNOWN_OUTCOME':0,'BREAKER_FALSE_POSITIVE_RATE':6/19,'BREAKER_TRUE_PROTECTIVE_RATE':13/19,'TRUE_PROTECTIVE_SAVED_LOSS_USDC':m['saved_loss'],'FALSE_POSITIVE_WHIPSAW_COST_USDC':m['whipsaw_cost'],'NET_BREAKER_VALUE_USDC':m['net_breaker_value'],'ACTUAL_POLICY_TOTAL_PNL':m['actual_total_pnl'],'NO_BREAKER_COUNTERFACTUAL_PNL':m['no_breaker_pnl'],'NO_BREAKER_DELTA_USDC':m['no_breaker_delta'],'CURRENT_LIKE_BREAKER_EXITS':3,'CURRENT_LIKE_NET_BREAKER_VALUE_USDC':-1.9134812513519996,'MEDIAN_BREAKER_DELTA_VS_HOLD':m['median_delta'],'MEAN_BREAKER_DELTA_VS_HOLD':m['mean_delta'],'MAX_FALSE_POSITIVE_BREAKER_COST':maximumcost,'MAX_TRUE_SAVED_LOSS':maximumsave,'NET_BREAKER_VALUE_PER_BREAKER_DAY':m['net_breaker_value']/m['breaker_days'],'NET_BREAKER_VALUE_PER_ALL_ACTIVE_DAY':m['net_breaker_value']/m['days']}
s['decision']={'ALL_HISTORY_DECISION':'MODIFY','CURRENT_POLICY_DECISION':'MODIFY','FINAL_DECISION':'MODIFY','PRIMARY_BREAKER_FAILURE_MODE':'CLASSIFICATION_ERROR; confirmation/execution delay secondary','NEXT_ACTION':'freeze existing benchmark; design and replay Breaker V2 in SHADOW ONLY; no runtime deployment','basis':'negative total, strict, broad, Sept, Oct; false-stop cost greater than saves; all leave-one-day-out remain negative','production_change_authorized':False}
s['robustness']={'leave_one_day_out_range': [min(lo),max(lo)],'all_breaker_event_days_negative':all(float(x['net_breaker_value'])<0 for x in daily if int(x['breaker_exits'])),'largest_day_fraction_abs_total':5.514481077161759/abs(m['net_breaker_value']),'strict_current_days':1,'current_LODO':'removing sole day leaves no evidence, not positive value','normalization':'all/strict/broad pooled net per notional and mean per share negative; near-zero legacy A mean per share flips slightly positive','excluded_nonbreaker_defect':'one Sept23 position fee/qty contradictory, excluded only portfolio level; no breaker conclusion affected','recent_venue_screenshot_difference':'17:15 screenshot buy4.40/sell1.25 implies about -3.15 vs journal -3.093; no fee/venue verification authorized; substituting screenshot makes breaker value ~0.057 worse, sign unchanged'}
inventory=[]
for path in (ROOT/'data/journal_archive/2026-09').glob('*.manifest.json'):
 a=json.loads(path.read_text());inventory.append({'manifest':str(path),'ORDER_FILLED':sum(t.get('per_event_type',{}).get('ORDER_FILLED',0) for t in a['tables'].values()),'ORDER_TAKER_EXIT_SUBMIT':sum(t.get('per_event_type',{}).get('ORDER_TAKER_EXIT_SUBMIT',0) for t in a['tables'].values())})
s['archive_inventory']=inventory
s['data_quality']={'actual_BUY_positions_examined':131,'economically_reconstructable_positions':130,'nonbreaker_fee_qty_conflict_excluded':1,'historical_synthetic_zero_price_fills_excluded':3,'unpaired_SELL_without_BUY':2,'historical_diagnostic_archives_additional_fills':0,'backup_additional_fills':0,'known_507_live_markets':'market/settlement record universe includes no actual BUY; not 507 positions','economic_scope':'local journal fills/fee ledger + verified official payout; not on-chain cash-account reconciliation','bootstrap_interpretation':'exploratory conditional 5 event days; no production confidence guarantee','official_margin':'not available from Gamma one-hot outcome; recent canonical TWAP explicitly secondary','TP_counterfactual':'hold benchmark excludes actual SELL fee; share-fee debit at entry preserved'}
# Full per-breaker compact table stays in CSV and report, sorted by economic delta.
bt=[]
for x in sorted(bb,key=lambda x:x['BREAKER_DELTA_VS_HOLD']):
 t=next(t for t in s['timing'] if t['market_slug']==x['market_slug'])
 bt.append([x['market_epoch'],x['Taipei_day'],x['held_side'],f"{x['ENTRY_VWAP']:.2f}",f"{x['ENTRY_QTY']:.4f}",f"{x['ENTRY_NOTIONAL']:.3f}",f"{x['exit_vwap']:.2f}",f"{x['ACTUAL_TOTAL_PNL']:+.3f}",x['OFFICIAL_OUTCOME'],x['HELD_SIDE_OFFICIAL_WIN'],f"{x['HOLD_TO_OFFICIAL_PNL']:+.3f}",f"{x['BREAKER_DELTA_VS_HOLD']:+.3f}",x['breaker_policy_cohort'],t['timing_quality']])
cs=s['cohort_sensitivity'];cstable=[[x['cohort'],x['positions'],x['days'],x['breaker_exits'],x['official_win_breaker_exits'],x['official_loss_breaker_exits'],f"{x['saved_loss']:.3f}",f"{x['whipsaw_cost']:.3f}",f"{x['net_breaker_value']:+.3f}",f"{100*x['net_per_initial_notional']:.2f}%" if x['net_per_initial_notional'] is not None else '—'] for x in cs]
report='''# 全歷史與近期 LIVE Breaker 經濟價值稽核

## 1. 最終判決：MODIFY

## 2. 一句話原因

13筆官方輸家共保住24.2577 USDC，但6筆官方贏家被切掉共損失33.8083，淨值−9.5507；嚴格current-like、寬鬆current-like、9月、10月都為負，逐日刪除後仍為負，故不能接受現有breaker原樣。這不是立即停用保護的授權。

## 3. 全部 LIVE 資料量與經濟重建

主表130筆actual LIVE position、14個台北日：9月124筆／13日；10月早上3筆與最新3筆共6筆／1日。官方方向獲勝96、失敗34；主表全部官方已驗證，unknown=0。資料單位market+held side，不是tick或episode；同市場同side跨run聚合。

熱DB共143列BUY fills，聚合131筆position；其中1筆非breaker的9/23費用／股數互相矛盾而排除經濟主表。2筆9/8、9/9SELL沒有 retained BUY，不能推測成本補入。3列零價格ORDER_FILLED與同order後續正價fill重複，按cash-trade排除，保留排除清單。131−1=130不是漏掉一筆breaker。備份263列fill、舊快照249列fill均無新增fill id；11日診斷archive manifests沒有ORDER_FILLED／breaker submit，無新actual exposure可補。507個LIVE市場包含無BUY的settlement/reference market，不是507持倉。

先讀project_overview.md，依BUY condition prefix與submit client_order_id修正SELL歸屬，16列instrument或slug異常離線歸正，DB未改。所有maker SELL仍MAKER_TP，沒有重新解讀成保護性退出。

費用：BUY raw qty扣share fee得到實際入庫qty；USDC entry fee／SELL fee各收一次，不把share fee再當USDC扣。Actual＝SELL cash proceeds＋官方殘量payout−entry cash cost−USDC fees。Realized按已賣部分成本配置，total再加殘量結算。Hold＝原始實際入庫qty×官方payoff−entry成本−entry費，**不扣假想SELL費**。全部是本地成交／費用ledger＋官方結果重建，不宣稱錢包流水已對帳。TP與hold的不同也包括省去SELL fee。

MARKET_CYCLE_PNL只作差異欄，不當真值。9/23 1790174700 raw BUY5.726028、share fee0.081259後只5.644769可用，SELL卻5.720302；還有BUY fill前已掛SELL、inventory增量異常，不能偷偷clamp成0然後叫可靠會計。這筆不是breaker，對breaker delta完全無影響；portfolio totals明確只含130筆可重建部位。

官方資料使用既有954市場版本cache、早上3市場補抓cache與最新3市場已驗證cache；沒有缺失，不需新網路。台北日以first entry local time分群，另保留UTC_day；weekend只作描述。

## 4. Breaker true saves vs false stops

確定有breaker成交19筆：官方輸家13（68.42%），官方贏家6（31.58%），unknown0。單純送單／被拒未成交不算breaker-exited position；一般offside/endgame/recovery退出另列OTHER_PROTECTIVE，沒有把舊報告所有taker stops都算absolute breaker。

TRUE_PROTECTIVE指官方輸家且退出帶來positive delta；本次13笔均正。FALSE_POSITIVE是官方贏家被breaker切掉，本次6筆delta均負。判斷不以actual PnL正負替代官方勝負。誤砍比例不是未來機率估計。

## 5. Breaker net economic value

保存損失24.257684；誤砍成本33.808348；**NET_BREAKER_VALUE=−9.550663 USDC**。
每筆平均−0.502666、中位+1.247828；平均負而中位正，顯示少數大額誤砍足以抵銷多筆小額保存，不能只看成功筆數。最大誤砍8.800315（9/27 1790474400），最大保存2.951685（9/28 1790536500）。每breaker-event日平均−1.910133、每active台北日平均−0.682190。

全部19筆逐position明細如下；run、git、journal差異與完整notes另保留於breaker_positions.csv。

'''+table(['市場epoch','台北日','side','entry','net股數','成本','exit VWAP','actual','官方','持有贏?','hold','delta','政策cohort','timing品質'],bt)+'''
## 6. No-breaker counterfactual

130筆實際總PnL **−27.163823**；僅將19筆breaker-exited position改用official hold benchmark，其餘TP／other protective／hold保持actual，counterfactual＝**−17.613160**，改善 **+9.550663**。

這是固定既有BUY與非breaker exits的會計benchmark，不是可行交易策略回測；忽略取消breaker後的資金占用、guard／後續entry變動、其它exit是否接手。因此正差額支持redesign，不能直接承諾實際關閉breaker多賺9.55。

## 7. Current-like cohort

STRICT＝10/10 early3＋latest3，共6position、3breaker；可用run manifest commit、safe config與STOP_TIMING鏡像驗證現行60s hold／$2 loss／trend／TTE或persistence-votes分支。其2筆true saves **+2.279657**、1筆false stop **−4.193138**，淨 **−1.913481**；actual−1.567529、no-breaker benchmark+0.345952。

BROAD＝strict＋first entry晚於f5a0f66 commit時間2026-09-29 21:46:33台北的歷史position；共17筆、4breaker，淨 **−0.428737**。這11筆9月部位deployment不明，不強迫歸入strict；其中1筆breaker可能仍跑舊政策，標unknown。使用它作寬鬆敏感度而非確定current-policy樣本。

SCHEMA_V2＝latest3、唯一breaker即17:15，淨−4.193138。它驗證誤砍存在，不單獨提供平均政策效果。後續ef5c45d/c6f454e/77cdfc5/94f1c47改的是telemetry／label／文件，不是新交易政策。

## 8. Breaker false-positive profile

6筆：9/25 1790343000、9/27 1790474400、9/28 1790543700／1790550000、9/29 1790685900、10/10 1791623700。成本約2.81–8.80 USDC，最後官方均是原held side贏。不是只有最新一筆才有whipsaw。

舊資料只能確認價位、qty、actual／hold、稀疏first-DD／spot crossings，沒有相容1s recovery／sigma／fresh L2。不能虛構六筆都有相同second-leg形狀。最新17:15 first0.10快速淺恢復→second→collapse；DDmax0.59、Binance約4.18bps、eligibility spot adverse而TWAP favorable；出場後bid0.99、spot/TWAP favorable recross。這直接證明目前確認仍可誤判，但不是可調新門檻的母體證據。

你提供的17:15venue截圖買4.40／賣收入1.25約意味−3.15，與本地ledger−3.093存在約0.057差異，未授權交易端點對帳，本次不猜原因。若改用截圖粗值，誤砍成本只會增加約0.057，MODIFY方向不變。

## 9. True-protective profile、near-tie與TP互動

13筆official loser退出均減少hold損失；保存總24.26，單筆最大2.95。早上兩筆UP official DOWN，saved1.355／0.925。它們確實有保護價值，不能因誤砍就說breaker毫無用處。

沒有完整可相容的false-vs-true高頻feature；僅最近3筆components較完整。Near-tie bins <=1／1–3／3–5／>5保留於CSV；Gamma cache不含official price margin。只有recent3 breaker有明確final canonical TWAP margin，3筆都<=1bps，卻包含2真保存和1誤砍；其餘16筆未知。因此 **無法證明誤砍集中near-tie或near-tie能區分真偽**。runtime canonical margin不是官方結果的price margin。

官方winner中TP／breaker／hold分開保留；breaker6筆actual低於hold合計33.81，TP通常保住大部分獲利，但沒有因果證據說這6筆如果不breaker就必定回到TP成交。不能說TP全球最佳。

## 10. Timing vs classification

6筆official winner存在CLASSIFICATION_ERROR，無論更早退出能否少虧。13筆loser退出可有policy／execution delay，但不改其正delta。

有eligible telemetry的早上兩筆：−2→eligible約88.33／62.74s；latest17:15為24.78s。早上第一筆initial submit被拒後重送，eligible→fill23.97s，不能只用成功單的1.56s假裝整段execution很快。第二筆eligible→fill2.34s；latest2.90s。最早submit、重送成功與fill按實際coid映射，不以全部run第一張submit亂配。

最新17:15最後瓶頸是trend確認，TTE已成立；但官方winner表示主經濟缺陷仍是分類。全歷史eligible缺失不能用submit替代，標UNKNOWN；timing_decomposition保留−1/−1.5/−2/−2.5、recorded net、censor與event品質。

## 11. Cohort／sizing／day sensitivity

'''+table(['cohort','positions','days','breaker','誤砍','真保存','saved','whipsaw','net','net/初始成本'],cstable)+'''
19筆delta按每股平均−0.063013、按各筆notional平均−8.6848%、聚合net／breaker entry notional＝−8.6434%；strict每股平均−0.115969、聚合notional−14.7418%；broad每股平均−0.049858、聚合notional−2.1897%。所以ALL/current結論不是被股數差異翻轉。

近零的legacy A僅2筆，USDC−0.149但每股均值略正+0.00584，這個小cohort的normalize方向敏感；不能隱藏，也不能凌駕ALL/current的負值。歷史A/B deployment僅commit-time推定，表列各段，未聲稱同一policy。

breaker-event台北日淨值：9/25−0.149、9/27−5.514、9/28−0.938、9/29−1.035、10/10−1.913；**五天全部負**。9/27占淨損失約57.7%，有集中，但刪除該日仍−4.036。全部14日逐一刪除後range **[−9.5507,−4.0362]**，沒有翻正。

5個independent breaker-event day做固定seed、10000次day-block bootstrap，只按日重抽，不按position/tick；探索性95%範圍約[−18.614,−3.210]。條件於已留存有breaker日，不代表未來保證；STRICT僅1日不做bootstrap，刪去唯一日是無資料，不是正值。MODIFY依據跨cohort經濟符號與LODO，不用小樣本一句話逃避決策。

## 12. 哪些資料可以合、哪些不能硬合

可合：actual BUY／SELL cashflow、正確fee/share ledger、verified official payout、breaker-minus-hold同一定義，按cohort另列normalized值。
不可硬合：legacy21s grid與v2約1.2s作相同first/second精度；缺失sigma/depth当0；舊stop全部當breaker；commit timestamp當deployment證明；非官方price margin當official；零價格synthetic fill當第二次賣出；fee/qty矛盾position硬clamp；507市場當507position。history與recent研究特徵只作secondary explanation，沒有新增規則搜尋。

## 13. 最終政策建議

ALL_HISTORY_DECISION=MODIFY；CURRENT_POLICY_DECISION=MODIFY；FINAL_DECISION=MODIFY。

KEEP不符合使用者規則：net不是正、whipsaw大於saved、strict/broad不是正。INCONCLUSIVE亦不合主證據：合理cohort和LODO沒有把ALL/current翻成KEEP，唯一unknown historical breaker即使移入broad仍負；唯一排除的非breaker費用問題不改breaker delta。這個判決是「不能接受現有設計原樣」，不是說可以關掉保護或已找到最佳替代。

## 14. 下一步唯一要做的事

凍結現行breaker為benchmark，**設計並對既有19個breaker事件離線重播純SHADOW Breaker V2**；優先處理classification／trend確認與恢复狀態，near-tie只作待檢驗背景，不依三筆調閾值。需同時保留13筆true saves並減少6筆false stops，對UNKNOWN深度不填假成交。通過既有事件比較後才另行規劃prospective shadow。此處只建議，沒有實作新邏輯或啟動實驗。

## 15. 明確列出不再做什麼

不再要求另一輪LIVE來迴避現有決策，不啟動LIVE/DRY-RUN，不下單/撤單，不立刻OFF，不改.env／threshold／TP／sizing／guards／settlement，不改DB歷史，不commit／push。不調新的停止門檻、不跑60-rule搜尋、不聲稱no-breaker benchmark是可交易政策或精確未來收益。

輸出包含要求8檔，另附identity correction／excluded ledger、reproduction script與sha256 manifest。DB始終mode=ro。所有政策結果只供研究檢討，不授予production SELL變更。

'''
report+='\n```text\n'+'\n'.join(k+'='+str(v) for k,v in s['required_numbers'].items())+'\nALL_HISTORY_DECISION=MODIFY\nCURRENT_POLICY_DECISION=MODIFY\nFINAL_DECISION=MODIFY\nPRIMARY_BREAKER_FAILURE_MODE=CLASSIFICATION_ERROR\nNEXT_ACTION=SHADOW_ONLY_BREAKER_V2_DESIGN_AND_REPLAY\nBOT_STOPPED_AT_END=YES\nNO_PUSH=YES\n```\n'
report=report.replace('13笔','13筆').replace('当','當').replace('恢复','恢復')
(O/'report.md').write_text(report);(O/'summary.json').write_text(json.dumps(s,indent=2,ensure_ascii=False))
for source in ['/tmp/consolidated_breaker.py','/tmp/consolidated_breaker_report.py']:(O/Path(source).name).write_bytes(Path(source).read_bytes())
(O/'manifest.json').write_text(json.dumps({x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in O.iterdir() if x.is_file() and x.name!='manifest.json'},indent=2))
print(json.dumps({'decision':s['decision'],'numbers':s['required_numbers'],'report_chars':len(report)},ensure_ascii=False))
