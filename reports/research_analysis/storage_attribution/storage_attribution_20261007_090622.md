# STORAGE ATTRIBUTION — 唯讀盤點

資料列率的共同觀測截止：2026-10-07T09:06:22.702391+08:00。報告完成：2026-10-07T09:15:31.154451+08:00。DB 仍由 bot 持續寫入；跨查詢不是同一 global snapshot。

## 方法、安全與限制
來源一律 URI mode=ro + query_only；沒有 cp、VACUUM、active WAL checkpoint、DELETE、schema 修改或 bot 操作。資料列以 rowid 上界固定並分批（每次 2,000 rows）讀取；每一批 statement 自有一致視圖。沒有新增 offline DB。Journal dbstat 採既有 atomic publish 的 backups/trade_journal.db（immutable read，開啟 inode 在替換後仍有效）；研究 DB dbstat 為唯讀 statement。sqlite dbstat aggregate 內部掃描不一定在 progress-handler 截止前返回，實測 journal backup 44.2s、現行 Hyperliquid 99.1s、舊 Hyperliquid 20.1s；未宣稱所有 statement 均在 12s 內完成。其間 bot 日誌仍有約 12–14 events/s、毫秒級 queue latency。
Payload 總 byte/count 為截止 rowid 的完整計數；median/P90/P99 為每97列加首50列 deterministic sample，非精確全量 quantile；max 是另外分批完整 length(CAST(... AS BLOB)) 掃描所得。Bytes/hour 基於真實 timestamp 最近1h；GiB/day = 24倍1h率，不是以 mtime 猜測。Table/index page大小依 dbstat；以近期 payload占比估計新增 table pages，無 payload表以近期 row占比；再按該 DB index/table比率補上 index 成本。這是 allocation estimate，非每一新頁的直接歸因。最近6h含停機，僅作敏感性比較，不適合作連續運行預測。無 freelist 不代表所有頁100%滿；dbstat unused屬 page內部空間，不能直接稱為可安全回收的完整 free pages。

## 1. 檔案與目錄占用
| 項目 | GiB |
| --- | --- |
| Filesystem free（完成時） | 16.607 |
| Repository allocated du（含 .git/.venv） | 23.777 |
| Repository logical（排除 .git/.venv/node_modules/cache） | 22.891 |
| data | 16.473 |
| backups | 2.675 |
| logs | 3.609 |
| analysis_snapshots | 11.519 |
| BTC1s Parquet logical | 0.050 |

Parquet small-part files 約4–5KiB，各自有 filesystem allocation/metadata overhead；du 約89MiB，logical約51MiB。歷史 public/unified Polymarket資料約80/147MiB，屬固定舊資料。最大歷史成本是4組 analysis snapshot DB，約11.5GiB；本次未 pin/unpin 或刪除。
### Runtime DB page / WAL / SHM
| 來源相對路徑 | DB GiB | WAL MiB | SHM MiB | page size | pages（初始） | free pages | used pages | free MiB | mode | tables | indexes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| logs/trade_journal.db | 2.678 | 1.084 | 0.031 | 4096 | 701326 | 0 | 701326 | 0.000 | wal | 6 | 7 |
| data/research/twap_forward_shadow.db | 0.968 | 0.114 | 0.031 | 4096 | 253514 | 0 | 253514 | 0.000 | wal | 6 | 6 |
| data/research/hyperliquid_lead_lag.db | 3.690 | 5.851 | 0.031 | 4096 | 966275 | 0 | 966275 | 0.000 | wal | 6 | 6 |
| logs/hyperliquid_lead_lag.db | 0.926 | 0.000 | 0.031 | 4096 | 242616 | 0 | 242616 | 0.000 | wal | 7 | 7 |

來源完整路徑為本 repo absolute root 加上上表路徑。dbstat journal分析來源：/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/backups/trade_journal.db；其他DB未建立snapshot，snapshot_path=NONE。WAL在觀測間會自然checkpoint，所以瞬間WAL量不能當日增長。所有額外 >100MiB SQLite檔案（backup/snapshots）如下：
| Snapshot / backup source path | GiB | page size | page count | freelist | mode | tables | indexes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/backups/trade_journal.db | 2.678 | 4096 | 701956 | 0 | delete | 6 | 7 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261006_064237_+0800/twap_forward_shadow_snapshot.db | 0.679 | 4096 | 177898 | 0 | delete | 6 | 6 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261006_064237_+0800/trade_journal_snapshot.db | 2.237 | 4096 | 586501 | 0 | delete | 6 | 7 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_185454_+0800/twap_forward_shadow_snapshot.db | 0.569 | 4096 | 149252 | 0 | delete | 6 | 6 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_185454_+0800/trade_journal_snapshot.db | 2.072 | 4096 | 543250 | 0 | delete | 6 | 7 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/freshness_audit_20261006_231849_+0800/twap_forward_shadow_snapshot.db | 0.896 | 4096 | 234895 | 0 | delete | 6 | 6 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/freshness_audit_20261006_231849_+0800/trade_journal_snapshot.db | 2.564 | 4096 | 672224 | 0 | delete | 6 | 7 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_065718_+0800/twap_forward_shadow_snapshot.db | 0.521 | 4096 | 136565 | 0 | delete | 6 | 6 |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_065718_+0800/trade_journal_snapshot.db | 1.979 | 4096 | 518886 | 0 | delete | 6 | 7 |

上述歷史副本僅做檔案/page inventory，不再重掃每份10+GiB重複內容；其table/index lineage分別對應journal與TWAP，但未把現在的大小冒充歷史精確table大小。
## 2. 每個 runtime DB 的 table/index attribution
### logs/trade_journal.db
dbstat object total=2.676GiB；index share=5.41%。
TOP20 TABLE（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| strategy_events | 1735.090 | 1.694 | 63.319 | 444183 |
| order_events | 855.926 | 0.836 | 31.236 | 219117 |
| strategy_runs | 0.824 | 0.001 | 0.030 | 211 |
| sqlite_sequence | 0.004 | 0.000 | 0.000 | 1 |
| journal_schema | 0.004 | 0.000 | 0.000 | 1 |
| session_pnl_state | 0.004 | 0.000 | 0.000 | 1 |

TOP20 INDEX（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| idx_strategy_events_run_ts | 64.926 | 0.063 | 2.369 | 16621 |
| idx_strategy_events_type_id | 35.109 | 0.034 | 1.281 | 8988 |
| idx_order_events_run_ts | 25.715 | 0.025 | 0.938 | 6583 |
| idx_order_events_client | 22.566 | 0.022 | 0.824 | 5777 |
| idx_order_events_fill_markout_buy_ts_id | 0.035 | 0.000 | 0.001 | 9 |
| sqlite_autoindex_strategy_runs_1 | 0.020 | 0.000 | 0.001 | 5 |
| sqlite_autoindex_session_pnl_state_1 | 0.004 | 0.000 | 0.000 | 1 |

索引prefix/duplicate候選：[]. 未找到同一table可直接判定重複的單列prefix/composite。idx_lead_lag_latency_name 名稱基數低但還含created_epoch_ns，不能因此判定冗餘；reference_1s/markouts autoindex維護唯一性，不能與普通查詢index混同。
| index | table | key columns |
| --- | --- | --- |
| sqlite_autoindex_strategy_runs_1 | strategy_runs | run_id |
| idx_order_events_run_ts | order_events | run_id, ts |
| idx_order_events_client | order_events | client_order_id |
| idx_strategy_events_run_ts | strategy_events | run_id, ts |
| idx_strategy_events_type_id | strategy_events | event_type, id |
| idx_order_events_fill_markout_buy_ts_id | order_events | ts, id |
| sqlite_autoindex_session_pnl_state_1 | session_pnl_state | session_date_taipei |

### Rows、日期與最近增長
| table | rows | table bytes/row | 最早Taipei | 最新Taipei | rows/h | rows/day | MiB/h table | GiB/day table |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_runs | 487 | 1774.653 | 2026-09-07T19:48:10.155065+08:00 | 2026-10-07T07:36:31.551070+08:00 | 0 | 0 | 0.000 | 0.000 |
| order_events | 362021 | 2479.147 | 2026-09-07T19:49:03.467770+08:00 | 2026-10-07T09:06:33.657718+08:00 | 2373 | 56952 | 5.155 | 0.121 |
| sqlite_sequence | 2 | 2048.000 | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| strategy_events | 913646 | 1991.333 | 2026-09-07T19:48:10.157490+08:00 | 2026-10-07T09:06:48.892637+08:00 | 4645 | 111480 | 14.427 | 0.338 |
| journal_schema | 1 | 4096.000 | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| session_pnl_state | 16 | 256.000 | 2026-09-28T22:00:28.523224+08:00 | 2026-10-07T07:35:15.053741+08:00 | 0 | 0 | 0.000 | 0.000 |

| table | payload avg B | median sample B | P90 sample B | P99 sample B | exact max B | sample N | gzip JSON sample reduction % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| strategy_runs | 1289.464 | 395 | 396 | 414 | 14603 | 55 | 93.578 |
| order_events | 1441.933 | 1568 | 2414 | 2870 | 5113 | 3782 | 95.160 |
| strategy_events | 1539.789 | 1002 | 2913 | 6305 | 30285 | 9469 | 89.456 |

### data/research/twap_forward_shadow.db
dbstat object total=0.967GiB；index share=0.46%。
TOP20 TABLE（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| lead_lag_decisions | 985.723 | 0.963 | 99.539 | 252345 |
| latency_spans | 0.004 | 0.000 | 0.000 | 1 |
| lead_lag_markouts | 0.004 | 0.000 | 0.000 | 1 |
| reference_1s | 0.004 | 0.000 | 0.000 | 1 |
| snapshots | 0.004 | 0.000 | 0.000 | 1 |
| sqlite_sequence | 0.004 | 0.000 | 0.000 | 1 |

TOP20 INDEX（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| idx_lead_lag_decision_time | 4.523 | 0.004 | 0.457 | 1158 |
| idx_lead_lag_latency_name | 0.004 | 0.000 | 0.000 | 1 |
| idx_lead_lag_markout_horizon_time | 0.004 | 0.000 | 0.000 | 1 |
| idx_lead_lag_scope_time | 0.004 | 0.000 | 0.000 | 1 |
| sqlite_autoindex_lead_lag_markouts_1 | 0.004 | 0.000 | 0.000 | 1 |
| sqlite_autoindex_reference_1s_1 | 0.004 | 0.000 | 0.000 | 1 |

索引prefix/duplicate候選：[]. 未找到同一table可直接判定重複的單列prefix/composite。idx_lead_lag_latency_name 名稱基數低但還含created_epoch_ns，不能因此判定冗餘；reference_1s/markouts autoindex維護唯一性，不能與普通查詢index混同。
| index | table | key columns |
| --- | --- | --- |
| idx_lead_lag_scope_time | snapshots | run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms |
| sqlite_autoindex_reference_1s_1 | reference_1s | run_id, slug, market_id, bucket_epoch_ms, source |
| sqlite_autoindex_lead_lag_markouts_1 | lead_lag_markouts | run_id, slug, market_id, candidate_epoch_ns, horizon_ms |
| idx_lead_lag_latency_name | latency_spans | name, created_epoch_ns |
| idx_lead_lag_markout_horizon_time | lead_lag_markouts | horizon_ms, candidate_epoch_ns |
| idx_lead_lag_decision_time | lead_lag_decisions | decision_epoch_ns |

### Rows、日期與最近增長
| table | rows | table bytes/row | 最早Taipei | 最新Taipei | rows/h | rows/day | MiB/h table | GiB/day table |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| snapshots | 0 | N/A | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| sqlite_sequence | 1 | 4096.000 | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| reference_1s | 0 | N/A | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| lead_lag_decisions | 244724 | 4223.554 | 2026-09-28T19:32:13.900967+08:00 | 2026-10-07T09:07:12.464997+08:00 | 3320 | 79680 | 13.825 | 0.324 |
| lead_lag_markouts | 0 | N/A | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| latency_spans | 0 | N/A | N/A | N/A | 0 | 0 | 0.000 | 0.000 |

| table | payload avg B | median sample B | P90 sample B | P99 sample B | exact max B | sample N | gzip JSON sample reduction % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lead_lag_decisions | 3549.592 | 3776 | 4031 | 4329 | 4542 | 2572 | 89.593 |

### data/research/hyperliquid_lead_lag.db
dbstat object total=3.687GiB；index share=16.45%。
TOP20 TABLE（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| lead_lag_decisions | 1912.824 | 1.868 | 50.661 | 489683 |
| latency_spans | 769.555 | 0.752 | 20.382 | 197006 |
| reference_1s | 249.062 | 0.243 | 6.596 | 63760 |
| snapshots | 194.844 | 0.190 | 5.160 | 49880 |
| lead_lag_markouts | 28.152 | 0.027 | 0.746 | 7207 |
| sqlite_sequence | 0.004 | 0.000 | 0.000 | 1 |

TOP20 INDEX（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| idx_lead_lag_latency_name | 385.148 | 0.376 | 10.201 | 98598 |
| sqlite_autoindex_reference_1s_1 | 183.977 | 0.180 | 4.873 | 47098 |
| idx_lead_lag_decision_time | 41.000 | 0.040 | 1.086 | 10496 |
| idx_lead_lag_scope_time | 7.250 | 0.007 | 0.192 | 1856 |
| sqlite_autoindex_lead_lag_markouts_1 | 3.059 | 0.003 | 0.081 | 783 |
| idx_lead_lag_markout_horizon_time | 0.836 | 0.001 | 0.022 | 214 |

索引prefix/duplicate候選：[]. 未找到同一table可直接判定重複的單列prefix/composite。idx_lead_lag_latency_name 名稱基數低但還含created_epoch_ns，不能因此判定冗餘；reference_1s/markouts autoindex維護唯一性，不能與普通查詢index混同。
| index | table | key columns |
| --- | --- | --- |
| idx_lead_lag_scope_time | snapshots | run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms |
| sqlite_autoindex_reference_1s_1 | reference_1s | run_id, slug, market_id, bucket_epoch_ms, source |
| sqlite_autoindex_lead_lag_markouts_1 | lead_lag_markouts | run_id, slug, market_id, candidate_epoch_ns, horizon_ms |
| idx_lead_lag_latency_name | latency_spans | name, created_epoch_ns |
| idx_lead_lag_markout_horizon_time | lead_lag_markouts | horizon_ms, candidate_epoch_ns |
| idx_lead_lag_decision_time | lead_lag_decisions | decision_epoch_ns |

### Rows、日期與最近增長
| table | rows | table bytes/row | 最早Taipei | 最新Taipei | rows/h | rows/day | MiB/h table | GiB/day table |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| snapshots | 98797 | 2067.962 | 2026-09-21T19:44:59.392000+08:00 | 2026-10-07T09:07:26.815000+08:00 | 662 | 15888 | 1.307 | 0.031 |
| sqlite_sequence | 4 | 1024.000 | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| reference_1s | 2125390 | 122.877 | 2026-09-21T19:44:52+08:00 | 2026-10-07T09:07:45+08:00 | 12563 | 301512 | 1.472 | 0.035 |
| lead_lag_decisions | 2203321 | 910.327 | 2026-09-21T19:44:52.178773+08:00 | 2026-10-07T09:08:01.605399+08:00 | 12372 | 296928 | 10.330 | 0.242 |
| lead_lag_markouts | 39859 | 740.607 | 2026-09-23T20:03:16.725570+08:00 | 2026-10-07T09:03:00.027514+08:00 | 192 | 4608 | 0.121 | 0.003 |
| latency_spans | 9072670 | 88.941 | 2026-09-21T19:44:52.178773+08:00 | 2026-10-07T09:08:44.397723+08:00 | 43232 | 1037568 | 3.667 | 0.086 |

| table | payload avg B | median sample B | P90 sample B | P99 sample B | exact max B | sample N | gzip JSON sample reduction % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| snapshots | 1924.959 | 1929 | 1960 | 1975 | 2007 | 1068 | 92.199 |
| lead_lag_decisions | 720.226 | 455 | 1596 | 1637 | 3458 | 22764 | 96.644 |
| lead_lag_markouts | 598.644 | 640 | 712 | 716 | 724 | 460 | 95.980 |

### logs/hyperliquid_lead_lag.db
dbstat object total=0.926GiB；index share=26.78%。
TOP20 TABLE（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| latency_spans | 348.941 | 0.341 | 36.819 | 89329 |
| lead_lag_decisions | 184.656 | 0.180 | 19.484 | 47272 |
| reference_1s | 87.238 | 0.085 | 9.205 | 22333 |
| snapshots | 67.078 | 0.066 | 7.078 | 17172 |
| lead_lag_markouts | 5.996 | 0.006 | 0.633 | 1535 |
| sqlite_sequence | 0.004 | 0.000 | 0.000 | 1 |
| lead_lag_archive_manifest | 0.004 | 0.000 | 0.000 | 1 |

TOP20 INDEX（不足20項則全列）
| object | MiB | GiB | % DB objects | pages |
| --- | --- | --- | --- | --- |
| idx_lead_lag_latency_name | 177.887 | 0.174 | 18.770 | 45539 |
| sqlite_autoindex_reference_1s_1 | 65.094 | 0.064 | 6.868 | 16664 |
| idx_lead_lag_decision_time | 7.582 | 0.007 | 0.800 | 1941 |
| idx_lead_lag_scope_time | 2.477 | 0.002 | 0.261 | 634 |
| sqlite_autoindex_lead_lag_markouts_1 | 0.590 | 0.001 | 0.062 | 151 |
| idx_lead_lag_markout_horizon_time | 0.164 | 0.000 | 0.017 | 42 |
| sqlite_autoindex_lead_lag_archive_manifest_1 | 0.004 | 0.000 | 0.000 | 1 |

索引prefix/duplicate候選：[]. 未找到同一table可直接判定重複的單列prefix/composite。idx_lead_lag_latency_name 名稱基數低但還含created_epoch_ns，不能因此判定冗餘；reference_1s/markouts autoindex維護唯一性，不能與普通查詢index混同。
| index | table | key columns |
| --- | --- | --- |
| idx_lead_lag_scope_time | snapshots | run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms |
| sqlite_autoindex_reference_1s_1 | reference_1s | run_id, slug, market_id, bucket_epoch_ms, source |
| sqlite_autoindex_lead_lag_markouts_1 | lead_lag_markouts | run_id, slug, market_id, candidate_epoch_ns, horizon_ms |
| idx_lead_lag_latency_name | latency_spans | name, created_epoch_ns |
| idx_lead_lag_markout_horizon_time | lead_lag_markouts | horizon_ms, candidate_epoch_ns |
| idx_lead_lag_decision_time | lead_lag_decisions | decision_epoch_ns |
| sqlite_autoindex_lead_lag_archive_manifest_1 | lead_lag_archive_manifest | table_name, partition_day |

### Rows、日期與最近增長
| table | rows | table bytes/row | 最早Taipei | 最新Taipei | rows/h | rows/day | MiB/h table | GiB/day table |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| snapshots | 34147 | 2059.815 | 2026-09-07T19:48:19.710000+08:00 | 2026-09-19T02:15:49.270000+08:00 | 0 | 0 | 0.000 | 0.000 |
| sqlite_sequence | 4 | 1024.000 | N/A | N/A | 0 | 0 | 0.000 | 0.000 |
| reference_1s | 753644 | 121.378 | 2026-09-07T19:48:12+08:00 | 2026-09-19T07:46:17+08:00 | 0 | 0 | 0.000 | 0.000 |
| lead_lag_decisions | 409047 | 473.359 | 2026-09-07T19:48:12.513268+08:00 | 2026-09-19T07:46:17.273752+08:00 | 0 | 0 | 0.000 | 0.000 |
| lead_lag_markouts | 7651 | 821.770 | 2026-09-07T19:51:40.631833+08:00 | 2026-09-09T06:21:49.204746+08:00 | 0 | 0 | 0.000 | 0.000 |
| latency_spans | 4231550 | 86.468 | 2026-09-07T19:48:12.513268+08:00 | 2026-09-19T07:46:17.574041+08:00 | 0 | 0 | 0.000 | 0.000 |
| lead_lag_archive_manifest | 0 | N/A | N/A | N/A | 0 | 0 | 0.000 | 0.000 |

| table | payload avg B | median sample B | P90 sample B | P99 sample B | exact max B | sample N | gzip JSON sample reduction % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| snapshots | 1652.211 | 1537 | 1954 | 1971 | 1997 | 402 | 90.497 |
| lead_lag_decisions | 380.502 | 375 | 427 | 457 | 573 | 4266 | 95.801 |
| lead_lag_markouts | 648.076 | 645 | 654 | 675 | 678 | 128 | 95.921 |

## 3. TradeJournal strategy_events event-type attribution
### TOP30 total payload bytes
| event | rows | % rows | payload MiB | % payload | avg B | rows/h | MiB/h payload | GiB/day payload |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ENTRY_DECISION_TRACE | 124344 | 13.610 | 352.059 | 26.241 | 2968.865 | 561 | 3.346 | 0.078 |
| QUOTE_TRANSPORT_TELEMETRY | 285869 | 31.289 | 309.339 | 23.057 | 1134.665 | 1428 | 2.850 | 0.067 |
| SIDE_DECISION_OBSERVATION | 34882 | 3.818 | 93.826 | 6.993 | 2820.481 | 209 | 0.563 | 0.013 |
| BUY_PATH_DIAGNOSTIC | 102570 | 11.226 | 88.278 | 6.580 | 902.465 | 662 | 0.459 | 0.011 |
| SIDE_DECISION | 32058 | 3.509 | 85.404 | 6.366 | 2793.464 | 329 | 0.876 | 0.021 |
| ENTRY_CONFIRMATION_OBSERVATION | 76763 | 8.402 | 74.371 | 5.543 | 1015.899 | 267 | 0.260 | 0.006 |
| SMART_MONEY_OBSERVATION | 76763 | 8.402 | 71.746 | 5.348 | 980.045 | 267 | 0.252 | 0.006 |
| LIVE_SIGNAL_COMPARE | 25969 | 2.842 | 55.175 | 4.112 | 2227.838 | 120 | 0.257 | 0.006 |
| SHADOW_SIGNAL_CANDIDATE_LIVE | 25566 | 2.798 | 54.406 | 4.055 | 2231.429 | 120 | 0.257 | 0.006 |
| MAIN_SIGNAL_CANDIDATE_LIVE | 20412 | 2.234 | 43.478 | 3.241 | 2233.497 | 98 | 0.210 | 0.005 |
| EVENT_LOOP_CONSUMER_TIMING | 1885 | 0.206 | 41.956 | 3.127 | 23338.796 | 60 | 1.491 | 0.035 |
| ENTRY_REGIME_OBSERVATION | 7309 | 0.800 | 17.925 | 1.336 | 2571.529 | 41 | 0.101 | 0.002 |
| ACCOUNT_SUMMARY | 32352 | 3.541 | 9.381 | 0.699 | 304.047 | 210 | 0.061 | 0.001 |
| FAST_FOLLOW_ENTRY_BLOCKED | 2276 | 0.249 | 3.435 | 0.256 | 1582.495 | 3 | 0.012 | 0.000 |
| FAST_FOLLOW_QUOTE_HANDOFF | 987 | 0.108 | 2.976 | 0.222 | 3161.813 | 3 | 0.012 | 0.000 |
| SIDE_MODE_CHANGED | 1034 | 0.113 | 2.761 | 0.206 | 2799.520 | 4 | 0.011 | 0.000 |
| FAST_FOLLOW_COUNTERFACTUAL_MARKOUT | 3996 | 0.437 | 2.697 | 0.201 | 707.786 | 12 | 0.008 | 0.000 |
| ENTRY_RESEARCH_TELEMETRY_SUMMARY | 5236 | 0.573 | 2.413 | 0.180 | 483.176 | 47 | 0.022 | 0.001 |
| SIDE_DECISION_SKIPPED | 2107 | 0.231 | 2.347 | 0.175 | 1168.090 | 2 | 0.002 | 0.000 |
| NO_TRADE_REDUCE_ONLY | 4611 | 0.505 | 2.173 | 0.162 | 494.083 | 24 | 0.011 | 0.000 |
| FAST_FOLLOW_CONFIRMED | 3076 | 0.337 | 2.153 | 0.160 | 733.817 | 3 | 0.002 | 0.000 |
| ENTRY_RESEARCH_CANDIDATE_TERMINAL | 3048 | 0.334 | 1.973 | 0.147 | 678.733 | 45 | 0.029 | 0.001 |
| NO_TRADE_ACTIVE_SIDE_NONE | 4625 | 0.506 | 1.915 | 0.143 | 434.160 | 23 | 0.010 | 0.000 |
| EXIT_POLICY_DECISION | 2259 | 0.247 | 1.591 | 0.119 | 738.619 | 0 | 0.000 | 0.000 |
| NO_TRADE_ECON_GATE | 2472 | 0.271 | 1.415 | 0.105 | 600.081 | 0 | 0.000 | 0.000 |
| MARKET_PHASE_CHANGE | 3888 | 0.426 | 1.230 | 0.092 | 331.756 | 16 | 0.005 | 0.000 |
| COLLECTION_LIFECYCLE | 492 | 0.054 | 1.040 | 0.078 | 2216.677 | 0 | 0.000 | 0.000 |
| EXECUTION_PENALTY_FALLBACK_APPLIED | 442 | 0.048 | 0.901 | 0.067 | 2137.982 | 0 | 0.000 | 0.000 |
| MARKET_STRIKE_PROVENANCE | 1248 | 0.137 | 0.890 | 0.066 | 747.980 | 5 | 0.004 | 0.000 |
| SIDE_MODE_FLIPPED | 290 | 0.032 | 0.801 | 0.060 | 2896.831 | 1 | 0.003 | 0.000 |

### TOP30 row count
| event | rows | % rows | payload MiB | % payload | avg B | rows/h | MiB/h payload | GiB/day payload |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QUOTE_TRANSPORT_TELEMETRY | 285869 | 31.289 | 309.339 | 23.057 | 1134.665 | 1428 | 2.850 | 0.067 |
| ENTRY_DECISION_TRACE | 124344 | 13.610 | 352.059 | 26.241 | 2968.865 | 561 | 3.346 | 0.078 |
| BUY_PATH_DIAGNOSTIC | 102570 | 11.226 | 88.278 | 6.580 | 902.465 | 662 | 0.459 | 0.011 |
| ENTRY_CONFIRMATION_OBSERVATION | 76763 | 8.402 | 74.371 | 5.543 | 1015.899 | 267 | 0.260 | 0.006 |
| SMART_MONEY_OBSERVATION | 76763 | 8.402 | 71.746 | 5.348 | 980.045 | 267 | 0.252 | 0.006 |
| SIDE_DECISION_OBSERVATION | 34882 | 3.818 | 93.826 | 6.993 | 2820.481 | 209 | 0.563 | 0.013 |
| ACCOUNT_SUMMARY | 32352 | 3.541 | 9.381 | 0.699 | 304.047 | 210 | 0.061 | 0.001 |
| SIDE_DECISION | 32058 | 3.509 | 85.404 | 6.366 | 2793.464 | 329 | 0.876 | 0.021 |
| LIVE_SIGNAL_COMPARE | 25969 | 2.842 | 55.175 | 4.112 | 2227.838 | 120 | 0.257 | 0.006 |
| SHADOW_SIGNAL_CANDIDATE_LIVE | 25566 | 2.798 | 54.406 | 4.055 | 2231.429 | 120 | 0.257 | 0.006 |
| MAIN_SIGNAL_CANDIDATE_LIVE | 20412 | 2.234 | 43.478 | 3.241 | 2233.497 | 98 | 0.210 | 0.005 |
| ENTRY_REGIME_OBSERVATION | 7309 | 0.800 | 17.925 | 1.336 | 2571.529 | 41 | 0.101 | 0.002 |
| ENTRY_RESEARCH_TELEMETRY_SUMMARY | 5236 | 0.573 | 2.413 | 0.180 | 483.176 | 47 | 0.022 | 0.001 |
| NO_TRADE_ACTIVE_SIDE_NONE | 4625 | 0.506 | 1.915 | 0.143 | 434.160 | 23 | 0.010 | 0.000 |
| NO_TRADE_REDUCE_ONLY | 4611 | 0.505 | 2.173 | 0.162 | 494.083 | 24 | 0.011 | 0.000 |
| FAST_FOLLOW_COUNTERFACTUAL_MARKOUT | 3996 | 0.437 | 2.697 | 0.201 | 707.786 | 12 | 0.008 | 0.000 |
| MARKET_PHASE_CHANGE | 3888 | 0.426 | 1.230 | 0.092 | 331.756 | 16 | 0.005 | 0.000 |
| FAST_FOLLOW_CONFIRMED | 3076 | 0.337 | 2.153 | 0.160 | 733.817 | 3 | 0.002 | 0.000 |
| ENTRY_RESEARCH_CANDIDATE_TERMINAL | 3048 | 0.334 | 1.973 | 0.147 | 678.733 | 45 | 0.029 | 0.001 |
| NO_TRADE_ECON_GATE | 2472 | 0.271 | 1.415 | 0.105 | 600.081 | 0 | 0.000 | 0.000 |
| FAST_FOLLOW_ENTRY_BLOCKED | 2276 | 0.249 | 3.435 | 0.256 | 1582.495 | 3 | 0.012 | 0.000 |
| EXIT_POLICY_DECISION | 2259 | 0.247 | 1.591 | 0.119 | 738.619 | 0 | 0.000 | 0.000 |
| SIDE_DECISION_SKIPPED | 2107 | 0.231 | 2.347 | 0.175 | 1168.090 | 2 | 0.002 | 0.000 |
| EVENT_LOOP_CONSUMER_TIMING | 1885 | 0.206 | 41.956 | 3.127 | 23338.796 | 60 | 1.491 | 0.035 |
| FAST_FOLLOW_EXPIRED | 1785 | 0.195 | 0.512 | 0.038 | 300.480 | 3 | 0.001 | 0.000 |
| MARKET_STRIKE_PROVISIONAL | 1615 | 0.177 | 0.555 | 0.041 | 360.195 | 2 | 0.001 | 0.000 |
| HYPERLIQUID_OUTCOME_OBSERVER_CONNECT_ATTEMPT | 1393 | 0.152 | 0.457 | 0.034 | 343.712 | 0 | 0.000 | 0.000 |
| HYPERLIQUID_OUTCOME_OBSERVER_CONNECTED | 1384 | 0.151 | 0.454 | 0.034 | 343.741 | 0 | 0.000 | 0.000 |
| HYPERLIQUID_OUTCOME_OBSERVER_SUBSCRIBED | 1384 | 0.151 | 0.498 | 0.037 | 377.153 | 0 | 0.000 | 0.000 |
| MARKET_STRIKE_PROVENANCE | 1248 | 0.137 | 0.890 | 0.066 | 747.980 | 5 | 0.004 | 0.000 |

最近strategy_events payload 50% threshold：ENTRY_DECISION_TRACE, QUOTE_TRANSPORT_TELEMETRY；累積55.55%。
最近strategy_events payload 80% threshold：ENTRY_DECISION_TRACE, QUOTE_TRANSPORT_TELEMETRY, EVENT_LOOP_CONSUMER_TIMING, SIDE_DECISION, SIDE_DECISION_OBSERVATION；累積81.80%。
最近strategy_events payload 95% threshold：ENTRY_DECISION_TRACE, QUOTE_TRANSPORT_TELEMETRY, EVENT_LOOP_CONSUMER_TIMING, SIDE_DECISION, SIDE_DECISION_OBSERVATION, BUY_PATH_DIAGNOSTIC, ENTRY_CONFIRMATION_OBSERVATION, LIVE_SIGNAL_COMPARE, SHADOW_SIGNAL_CANDIDATE_LIVE, SMART_MONEY_OBSERVATION；累積95.12%。
注意上面growth share分母是strategy_events JSON，不是整個journal實體DB（後者還含order_events與indexes）。ENTRY_DECISION_TRACE當前payload share約30%；歷史total payload share約26.2%。EVENT_LOOP_CONSUMER_TIMING只有60 rows/h，但avg最近約26KiB，每小時約1.49MiB，頻率低不代表儲存小。
### 指定operational telemetry搜尋（exact type）
| family | matching event types |
| --- | --- |
| STATUS | 無同名type；不能據此斷言其他DB/console沒有 |
| HEART | 無同名type；不能據此斷言其他DB/console沒有 |
| QUOTE | QUOTE_TRANSPORT_TELEMETRY, QUOTE_WATCHDOG_TRIGGERED, QUOTE_WATCHDOG_RESUBSCRIBED, QUOTE_WATCHDOG_NODE_ROLLOVER, FAST_FOLLOW_QUOTE_HANDOFF, QUOTE_MARKET_HANDOFF_READY, QUOTE_WATCHDOG_ROLLOVER_DEFERRED_PROTECTIVE_SELL, QUOTE_WATCHDOG_FAILED, QUOTE_WATCHDOG_PARTIAL_MARKET_DATA, QUOTE_MARKET_HANDOFF_PREWARM_READY, QUOTE_WATCHDOG_TRANSPORT_ALIVE_DATA_STALE, QUOTE_SUBSCRIPTION_RECONCILIATION, QUOTE_WATCHDOG_REFRESH_REQUESTED, QUOTE_REFRESH_REQUESTED, QUOTE_REFRESH_FRESH_QUOTE_CONFIRMED, QUOTE_REFRESH_TASK_STARTED, QUOTE_REFRESH_TASK_COMPLETED |
| PREDICTION | 無同名type；不能據此斷言其他DB/console沒有 |
| DATAENGINE | 無同名type；不能據此斷言其他DB/console沒有 |
| STORAGE | 無同名type；不能據此斷言其他DB/console沒有 |
| WATCHDOG | QUOTE_WATCHDOG_TRIGGERED, QUOTE_WATCHDOG_RESUBSCRIBED, QUOTE_WATCHDOG_NODE_ROLLOVER, QUOTE_WATCHDOG_ROLLOVER_DEFERRED_PROTECTIVE_SELL, QUOTE_WATCHDOG_FAILED, QUOTE_WATCHDOG_PARTIAL_MARKET_DATA, QUOTE_WATCHDOG_TRANSPORT_ALIVE_DATA_STALE, QUOTE_WATCHDOG_REFRESH_REQUESTED |
| MAKER | 無同名type；不能據此斷言其他DB/console沒有 |
| NO_QUOTE | 無同名type；不能據此斷言其他DB/console沒有 |
| FAST_FOLLOW | FAST_FOLLOW_CONFIRMED, FAST_FOLLOW_EXPIRED, FAST_FOLLOW_RISK_STATE, FAST_FOLLOW_REVERSAL_EXIT_SUBMITTED, FAST_FOLLOW_ENTRY_BLOCKED, FAST_FOLLOW_OVERFILL_ACCEPTED, OUTCOME_FAST_FOLLOW_EXECUTION_PENALTY_CALIBRATED, FAST_FOLLOW_QUOTE_HANDOFF, FAST_FOLLOW_COUNTERFACTUAL_ENTRY, FAST_FOLLOW_COUNTERFACTUAL_MARKOUT, FAST_FOLLOW_COUNTERFACTUAL_INCOMPLETE, FAST_FOLLOW_ERROR |
| OUTCOME | HYPERLIQUID_OUTCOME_OBSERVER_STARTED, EXIT_AUDIT_OUTCOME, HYPERLIQUID_OUTCOME_OBSERVER_CONNECT_ATTEMPT, HYPERLIQUID_OUTCOME_OBSERVER_CONNECTED, HYPERLIQUID_OUTCOME_OBSERVER_SUBSCRIBED, HYPERLIQUID_OUTCOME_OBSERVER_DISCONNECTED, HYPERLIQUID_OUTCOME_OBSERVER_STABLE, OUTCOME_FAST_FOLLOW_EXECUTION_PENALTY_CALIBRATED |
| POSITION | 無同名type；不能據此斷言其他DB/console沒有 |
| BBO | 無同名type；不能據此斷言其他DB/console沒有 |
| HEALTH | 無同名type；不能據此斷言其他DB/console沒有 |

### 重複 metadata 估計（sample，非已實現節省）
| DB | table | sample fields | estimated repeated B/row | upper-bound MiB/day | payload % |
| --- | --- | --- | --- | --- | --- |
| logs/trade_journal.db | strategy_runs | instrument_id | 126.491 | 0.000 | 9.810 |
| logs/trade_journal.db | order_events | slug,instrument_id,market_slug | 251.068 | 13.636 | 17.412 |
| logs/trade_journal.db | strategy_events | slug,market_slug,instrument_id,git_revision,source_fingerprint,reference_source,run_id | 250.284 | 26.609 | 16.254 |
| data/research/twap_forward_shadow.db | lead_lag_decisions | market_slug,probability_model_version,run_id,prediction_schema_version,research_schema_version,instrument_id | 159.081 | 12.088 | 4.482 |
| data/research/hyperliquid_lead_lag.db | snapshots | slug,reference_source,hyperliquid_outcome_source,research_schema_version | 154.139 | 2.336 | 8.007 |
| data/research/hyperliquid_lead_lag.db | lead_lag_decisions | feature_version,slug,instrument_id,run_id,reference_source,research_schema_version | 115.176 | 32.615 | 15.992 |
| data/research/hyperliquid_lead_lag.db | lead_lag_markouts | slug | 14.685 | 0.065 | 2.453 |
| logs/hyperliquid_lead_lag.db | snapshots | slug,reference_source,hyperliquid_outcome_source | 148.905 | 0.000 | 9.012 |
| logs/hyperliquid_lead_lag.db | lead_lag_decisions | feature_version | 50.904 | 0.000 | 13.378 |
| logs/hyperliquid_lead_lag.db | lead_lag_markouts |  | 0.000 | 0.000 | 0.000 |

這些欄位不是逐byte都可刪除：保留join keys/version與dim metadata有成本；表外run/slug也重複且不在此JSON estimate中。Config/SHA大多集中少量strategy_runs（487列、約0.6MiB JSON），不是主因。Nested instrument state以及JSON field names還有額外可壓縮重複；gzip結果證實冗餘，但不能直接等同normalization節省。
## 4. TWAP / research：500MiB cap來源
cap檢查main+WAL+SHM，不是per-table cap。啟動durable event報告970.613MiB、configured_max_db_mb=500、trigger_reason=db_size_cap。最大table lead_lag_decisions約985.7MiB，占DB約99.5%，因此實際主要貢獻表是它。
| event | rows | payload MiB | payload % | rows/h | MiB/h |
| --- | --- | --- | --- | --- | --- |
| PREDICTION_RESEARCH_SNAPSHOT | 200187 | 727.690 | 87.840 | 2696 | 11.009 |
| SETTLEMENT_PATH_THRESHOLD_CROSS | 12433 | 47.549 | 5.740 | 0 | 0.000 |
| DECISION_POINT_L2 | 17997 | 17.701 | 2.137 | 561 | 0.551 |
| TWAP_PROJECTED_SIDE_CHANGE | 5494 | 17.392 | 2.099 | 0 | 0.000 |
| MARKET_OPENING_TWAP_SAMPLE | 4312 | 9.378 | 1.132 | 0 | 0.000 |
| TMINUS_CHECKPOINT | 1683 | 5.358 | 0.647 | 0 | 0.000 |
| PREDICTION_RESEARCH_HEALTH | 1735 | 1.382 | 0.167 | 59 | 0.052 |
| TWAP_STRIKE_CROSS | 381 | 1.280 | 0.155 | 0 | 0.000 |
| MARKET_TWAP_SUMMARY | 410 | 0.668 | 0.081 | 4 | 0.007 |
| RESEARCH_STORAGE_GUARD_TRIGGERED | 92 | 0.031 | 0.004 | 0 | 0.000 |

PREDICTION_RESEARCH_SNAPSHOT為高頻同步evidence，而非settlement summaries；prediction仍繼續寫入，TWAP guard只抑制該observer optional事件，故DB仍增長。Snapshots/reference_1s/latency表此DB為空，不能把Hyperliquid raw源混入TWAP成本。Settlement summaries很小（上表逐event精確值）。
理論選項：A rollover單獨=0%總儲存節省，只能界定active DB；B normalization採上方重複metadata estimate，非直接承諾；C operational aggregation僅能影響被選事件而非主要prediction；D冷壓縮sample JSON節省89.6%、page-block87.9%，實際archive需獨立驗證；E prediction 2x downsampling最多減少約44% JSON（prediction約87.8% ×50%），會損失gap/repricing/as-of證據，不建議在現研究中直接做。
## 5. Hyperliquid / Outcome attribution
| reference source | rows | rows/h | row share % |
| --- | --- | --- | --- |
| polymarket_twap | 508738 | 2860 | 23.936 |
| binance | 550640 | 3068 | 25.908 |
| polymarket_spot | 486960 | 2700 | 22.912 |
| polymarket_bbo | 457612 | 3221 | 21.531 |
| outcome_btc_mark | 115861 | 714 | 5.451 |
| hyperliquid_btc_l2book | 876 | 0 | 0.041 |
| hyperliquid_btc_bbo | 4703 | 0 | 0.221 |

Raw reference_1s是多源混合：Binance、Polymarket TWAP/spot/BBO與Outcome BTC mark。沒有一整張表可稱為raw Hyperliquid BTC。outcome_btc_mark row比例約5.45%；按同等bytes/row分配只屬粗估，不是page逐源精確歸因。Snapshots合存reference與Outcome contract side mids/BBO/depth；無法乾淨拆分為兩套物理頁，不能double-count。
| category | table / payload authority | MiB | % DB | precision |
| --- | --- | --- | --- | --- |
| mixed Outcome/reference observations | snapshots | 194.844 | 5.160 | whole table exact; source split not separable |
| multi-source BTC/BBO reference | reference_1s | 249.062 | 6.596 | whole table exact |
| derived decisions + shadow marks/BBO | lead_lag_decisions | 1912.824 | 50.661 | mixed shared writer |
| derived markouts | lead_lag_markouts | 28.152 | 0.746 | exact table |
| timing diagnostics | latency_spans | 769.555 | 20.382 | exact table |
| all indexes | index data | 621.270 | 16.454 | exact objects |

| decision event | rows | payload MiB | rows/h | MiB/h |
| --- | --- | --- | --- | --- |
| SHADOW_POSITION_MARK | 452602 | 687.599 | 1994 | 3.074 |
| UNCLASSIFIED | 1393759 | 581.216 | 8198 | 3.621 |
| SHADOW_BBO_MATERIAL_CHANGE | 215864 | 111.389 | 1421 | 0.758 |
| SHADOW_BBO_SNAPSHOT | 120040 | 109.166 | 630 | 0.571 |
| TREND_ENTRY_SHADOW_CANDIDATE | 5704 | 7.456 | 48 | 0.064 |
| SHADOW_ENTRY_CANDIDATE | 1959 | 6.141 | 12 | 0.038 |
| SHADOW_SETTLEMENT | 4788 | 4.987 | 12 | 0.013 |
| SHADOW_EXIT | 4198 | 3.141 | 24 | 0.018 |
| TREND_ENTRY_SHADOW_SETTLEMENT | 2634 | 1.267 | 24 | 0.012 |
| SHADOW_LOSS_WARNING | 823 | 0.403 | 6 | 0.003 |
| FORWARD_SHADOW_CAPTURE_HEALTH | 407 | 0.218 | 1 | 0.001 |
| SHADOW_SIGNAL_REVERSAL | 421 | 0.150 | 2 | 0.001 |
| STOP_SHADOW_CHECKPOINT | 40 | 0.118 | 0 | 0.000 |
| STOP_SHADOW_CANDIDATE | 33 | 0.101 | 0 | 0.000 |
| STOP_SHADOW_ADVERSE_EPISODE_STARTED | 21 | 0.011 | 0 | 0.000 |
| MARKET_TWAP_SUMMARY | 9 | 0.006 | 0 | 0.000 |
| STOP_SHADOW_ADVERSE_EPISODE_CLEARED | 10 | 0.003 | 0 | 0.000 |
| RESEARCH_STORAGE_GUARD_TRIGGERED | 7 | 0.002 | 0 | 0.000 |
| STOP_SHADOW_POST_STOP_SETTLEMENT | 1 | 0.001 | 0 | 0.000 |
| STOP_SHADOW_ACTUAL_STOP | 1 | 0.001 | 0 | 0.000 |

UNCLASSIFIED只是沒有event_type，sample payload含 state/reason/source/feature_version=outcome_lead_lag_v4_entry_only，屬derived observation；不能誤稱成未知raw資料。SHADOW_POSITION_MARK約721MB JSON，遠大於純Outcome行情占用。Fast-follow若未單列event，不能將整張shared表歸入fast-follow。目前recent timestamps與1h新增列證明data/research版本正在寫入；logs版本recent1h=0，最新日期見row表，屬歷史路徑。
| latency name | all rows | recent1h rows |
| --- | --- | --- |
| binance_to_decision | 5631486 | 18190 |
| polymarket_bbo_to_decision | 2133082 | 15094 |
| polymarket_spot_to_decision | 597314 | 3483 |
| polymarket_twap_to_decision | 597271 | 3487 |
| outcome_btc_mark_to_decision | 115925 | 715 |
| order_handoff | 333 | 0 |
| cancel_request_to_ack | 143 | 0 |

## 6. Growth forecast：allocation估計，不是實測未來
| DB | current GiB | GiB/day 1h rate | 24h GiB | 7d GiB | 30d GiB | 6h sensitivity GiB/day |
| --- | --- | --- | --- | --- | --- | --- |
| logs/trade_journal.db | 2.678 | 0.485 | 3.163 | 6.074 | 17.234 | 0.157 |
| data/research/twap_forward_shadow.db | 0.968 | 0.326 | 1.294 | 3.247 | 10.734 | 0.101 |
| data/research/hyperliquid_lead_lag.db | 3.690 | 0.474 | 4.164 | 7.008 | 17.911 | 0.161 |
| logs/hyperliquid_lead_lag.db | 0.926 | 0.000 | 0.926 | 0.926 | 0.926 | 0.000 |

Primary DB合計≈1.285GiB/day；單一published backup image增長≈0.485GiB/day（不是每30秒永久多留完整image）。Operational file log本run extrapolation≈0.009GiB/day；BTC1s歷史完整日logical≈0.012GiB/day。總managed logical baseline≈1.791GiB/day；7d/30d新增≈12.537/53.732GiB，不包含新人工snapshots/APFS外部保留或archive再複製。
條件projection：以目前free=16.607GiB和managed rate，至10GiB free約3.69天，至0約9.27天。這不是保證；空間可因APFS purge、外部程序及backup snapshot COW突然改變。
## 7. 為什麼free disk下降比DB成長快？
Runtime telemetry 07:35 free27.32GiB，07:55 23.09，08:15 19.27，08:55 16.95；本盤點開始約13.68，後來約14.53：不是穩定直線，已有反向波動。三個active DB估計合計約1.285GiB/day，只是約0.054GiB/h，不能解釋filesystem每小時數GiB的下降。
已唯讀確認Data volume有Time Machine local snapshots 08:00:57與09:00:57，均Purgeable。Atomic backup每30秒重写約2.68GiB，即使directory只保留1份，APFS snapshot可能保留先前image區塊；這是機制上的候选，不是已量測的snapshot bytes歸因。bot lsof +L1未见大型deleted/unlinked DB handle。尚未获得snapshot獨占空間及repository外所有进程寫入歸因，故快速filesystem耗用的exact root cause仍未完全确认。不可用1.8GiB/day的logical預測声稱磁碟安全，也不可拿几GiB/h短期APFS波动當30天稳定DB增长率。
## 8. Optimization ranking — 僅建議，未實作
| priority | opportunity | potential reduction | research loss | risk / qualification |
| --- | --- | --- | --- | --- |
| HIGH IMPACT / LOW RISK | verified cold archives + compression | 80–88% cold SQLite sample; about89–97%JSON block sample | NONE | immutable archival+manifest+restore驗證；sample不是保證；現有pinned不能自行轉換 |
| HIGH IMPACT / MEDIUM RISK | aggregate operational latency/health telemetry | latency table+index約30.6% Hyperliquid DB；聚合掉90%則約27.5%整DB | LOW–MEDIUM | 保留rare slow traces與分位數；loss of exact replay/timing events |
| HIGH IMPACT / MEDIUM RISK | aggregate journal trace/quote/timing or state-change only | recent top2占strategy JSON55.5%、top3 68.9%；90%aggregation約50–62%該stream | LOW–MEDIUM | 不能把entry provenance與策略decision每次记錄一概刪去 |
| MEDIUM IMPACT / LOW RISK | normalize static identifiers/schema metadata | 依各table sample上限表；先做lossless dimension references | NONE | schema/version/migration消费者兼容性需另驗證，非零implementation risk |
| MEDIUM IMPACT / LOW RISK | DB rollover + verified cold archival | rollover alone0%；archive compression另計 | NONE | active DB bounded，總archive仍需retention；readonly comparison需跨partition |
| LOW VALUE | drop presumed redundant indexes | 已證實duplicate/prefix=0%；不可把所有index當可刪除 | NONE if proven | journal所有index僅5.4%；unique/partial索引不可直接刪 |
| HIGH IMPACT / MEDIUM RISK | remove Outcome subsystem wholesale | 不是整個shared DB100%；mixed snapshots+Outcome portion需先拆source | HIGH | BTC/reference/lead-lag plusshadow同DB，不可把shadow evidence誤刪 |
| HIGH IMPACT / MEDIUM RISK | prediction 2x downsampling | 約44% TWAP payload理论值 | HIGH | 會改變gap/repricing/as-of解析；不适合本研究直接优化 |

## 9. Safety與產物
CODE_CHANGED=NO；ACTIVE_DATA_MUTATED_BY_ANALYSIS=NO；BOT_RESTARTED=NO；COMMIT/PUSH=NO；DELETE/VACUUM/MANUAL_CHECKPOINT=NO。Bot自身合法寫入在盘点期間繼續。報告與JSON均為local untracked。详细JSON保存全部events、source schema、samples與rates；object sidecars记錄dbstat，context记錄protected舊snapshot inventory。
## 補充：order_events 與 journal 全 stream

最近1h order_events payload=2.998MiB；strategy_events=11.155MiB。兩者合計分母下，strategy_events占78.82%。不要把strategy_events內的event百分比當成整個journal成長百分比。

| event | rows | payload MiB | avg payload B | rows/h | MiB/h |
|---|---:|---:|---:|---:|---:|
| ENTRY_EDGE_OBSERVATION | 150143 | 350.913 | 2450.7 | 828 | 1.913 |
| ORDER_OBSERVE_BUY_BLOCKED | 29184 | 46.346 | 1665.2 | 101 | 0.162 |
| DEPTH_RISK_SHADOW_MARKOUT | 64285 | 45.303 | 739.0 | 630 | 0.444 |
| DEPTH_RISK_SHADOW_CANDIDATE | 16225 | 15.825 | 1022.7 | 160 | 0.156 |
| ORDER_SKIP_LOCKED_SIDE_INVALIDATED | 21258 | 6.923 | 341.5 | 67 | 0.022 |
| ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE | 18675 | 6.559 | 368.3 | 197 | 0.069 |
| ORDER_SKIP_DIRECTIONAL_ENTRY_GATE | 15910 | 5.045 | 332.5 | 203 | 0.064 |
| ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW | 15321 | 5.040 | 344.9 | 90 | 0.030 |
| ORDER_SKIP_MARKET_BUY_LIMIT | 10351 | 3.780 | 383.0 | 0 | 0.000 |
| ORDER_SKIP_FAST_FOLLOW_OWNERSHIP | 9056 | 2.220 | 257.1 | 0 | 0.000 |
| SHADOW_SIM_ENTRY_CANCELLED | 560 | 1.368 | 2560.6 | 18 | 0.044 |
| FILL_MARKOUT | 1016 | 1.359 | 1402.8 | 0 | 0.000 |
| SHADOW_SIM_ENTRY_REQUOTED | 499 | 1.207 | 2535.3 | 18 | 0.043 |
| ORDER_SKIP_MARKET_STOP_LOSS_LIMIT | 2597 | 0.802 | 324.0 | 0 | 0.000 |
| ORDER_SUBMIT | 373 | 0.767 | 2156.5 | 0 | 0.000 |
| SHADOW_SIM_ENTRY_CANDIDATE | 315 | 0.731 | 2431.9 | 4 | 0.009 |
| ORDER_SKIP_TWAP_REFERENCE_DEGRADED | 2220 | 0.616 | 291.0 | 4 | 0.001 |
| SHADOW_SIM_ENTRY_FILLED | 245 | 0.614 | 2628.9 | 3 | 0.008 |
| SHADOW_SIM_SETTLED | 215 | 0.593 | 2893.0 | 4 | 0.011 |
| ORDER_DRY_RUN_SUBMITTED | 826 | 0.457 | 579.7 | 22 | 0.011 |
| ORDER_DRY_RUN_CANCELLED | 568 | 0.246 | 454.0 | 18 | 0.008 |
| SHADOW_SIM_MARKOUT | 538 | 0.244 | 475.9 | 6 | 0.003 |
| ORDER_FILLED | 249 | 0.242 | 1020.6 | 0 | 0.000 |
| ORDER_SKIP_SELL_DELAY_AFTER_BUY | 423 | 0.147 | 364.4 | 0 | 0.000 |
| ORDER_MAKER_INTENT | 241 | 0.144 | 626.9 | 0 | 0.000 |
| ORDER_FAST_FOLLOW_INTENT | 27 | 0.075 | 2895.3 | 0 | 0.000 |
| ORDER_FAST_FOLLOW_SUBMIT | 65 | 0.049 | 785.0 | 0 | 0.000 |
| ORDER_CANCELED | 163 | 0.040 | 257.0 | 0 | 0.000 |
| ORDER_TAKER_EXIT_SUBMIT | 32 | 0.029 | 947.9 | 0 | 0.000 |
| ORDER_SKIP_PENDING_TAKER_EXIT | 88 | 0.028 | 330.0 | 0 | 0.000 |

Forecast 1.791GiB/day 包含單一 backup 的 size growth；不包含每次 rewrite 導致 APFS snapshot 保留舊區塊的額外成本。free disk 較 context 觀測的14.53GiB又回升到報告完成時16.607GiB，進一步證實不能對短期free差額作穩定線性外推。

## 10. Required summary
Event share分母為最近1h strategy_events payload；day rate是条件allocation estimate；days-to-10為managed logical growth情境。
```text
STORAGE_ATTRIBUTION
FILESYSTEM_FREE_GIB=16.607
TRADE_JOURNAL_GIB=2.678
TRADE_JOURNAL_GIB_PER_DAY=0.485
TRADE_JOURNAL_TOP_TABLE=strategy_events
TRADE_JOURNAL_TOP_EVENT_TYPE=ENTRY_DECISION_TRACE
TRADE_JOURNAL_TOP_EVENT_SHARE_PCT=29.997
TRADE_JOURNAL_INDEX_SHARE_PCT=5.415
TWAP_RESEARCH_GIB=0.968
TWAP_RESEARCH_GIB_PER_DAY=0.326
TWAP_TOP_TABLE=lead_lag_decisions
TWAP_DB_SIZE_CAP_TRIGGER_TABLE=lead_lag_decisions (DB-wide cap)
HYPERLIQUID_DB_GIB=3.690
HYPERLIQUID_GIB_PER_DAY=0.474
HYPERLIQUID_CURRENTLY_WRITING=YES
HYPERLIQUID_TOP_TABLE=lead_lag_decisions
TOTAL_ESTIMATED_GIB_PER_DAY=1.791
PROJECTED_DAYS_TO_10_GIB_FREE=3.689
TOP_STORAGE_ROOT_CAUSE=repository: retained snapshots + high-volume JSON/latency history; rapid filesystem loss not fully attributed (APFS snapshot COW candidate)
TOP_LOW_RISK_OPTIMIZATION=verified cold archive compression preserving original evidence
TOP_HIGH_IMPACT_OPTIMIZATION=aggregate operational timing/quote telemetry while retaining exceptional traces
CODE_CHANGED=NO
ACTIVE_DATA_MUTATED=NO
BOT_RESTARTED=NO
```
