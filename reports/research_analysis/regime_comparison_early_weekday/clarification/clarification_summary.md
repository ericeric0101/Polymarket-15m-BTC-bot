# EARLY REGIME COMPARISON — CLARIFICATION PASS

Baseline verified at `c8eeaaacb660a32fd1be305115ed31fd1c67c5fa`. All database reads below used only the two named offline snapshots. The previous report’s generated CSVs were used only as the established synchronized cohort membership; payload and row-level clarifications below were recalculated from the snapshots.

## 1. Rows vs runs vs markets

| Scope | Rows | Unique run IDs | Unique markets | Meaning |
|---|---:|---:|---:|---|
| journal `strategy_runs` | 404 | 404 | — | Run registry rows; duplicate run IDs: 0 |
| research `lead_lag_decisions` | 128399 | 128 | 313 | All event types |
| `PREDICTION_RESEARCH_SNAPSHOT` rows | 103836 | 72 | 198 | Relevant synchronized-prediction stream; 404 is not this row count |
| Canonical settlement summaries | 250 | 98 | 250 | All summary rows in research DB |

The prior “404 recorded runs” refers to **404 strategy_runs rows and 404 distinct run IDs**, not 404 research rows. The provenance join counted 404 registry run IDs as legacy/unmanifested. Only 72 run IDs contain prediction snapshot rows; all-record research has 128 IDs. `0` registry entries contain a run manifest.

Per-run detail below covers every run ID with any row in `lead_lag_decisions` (decision-row count includes all event types; prediction-row count is the subset). All timestamps are Asia/Taipei.

| Run ID | Decision rows | Prediction rows | Markets | Weekday | Weekend | First | Last |
|---|---:|---:|---:|---:|---:|---|---|
| `run_1790595059_5ea76b48` | 139 | 0 | 4 | 4 | 0 | 2026-09-28T19:32:13.900967+08:00 | 2026-09-28T20:19:34.231300+08:00 |
| `run_1790598051_42e47607` | 45 | 0 | 1 | 1 | 0 | 2026-09-28T20:21:54.809157+08:00 | 2026-09-28T20:30:01.800809+08:00 |
| `run_1790598893_ce583deb` | 19 | 0 | 1 | 1 | 0 | 2026-09-28T20:36:03.529385+08:00 | 2026-09-28T20:41:01.650185+08:00 |
| `run_1790599336_35380309` | 176 | 0 | 5 | 5 | 0 | 2026-09-28T20:43:10.173610+08:00 | 2026-09-28T21:32:31.968005+08:00 |
| `run_1790602486_1227f34f` | 24 | 0 | 1 | 1 | 0 | 2026-09-28T21:35:50.915218+08:00 | 2026-09-28T21:41:55.881642+08:00 |
| `run_1790603973_51d3c8ab` | 16 | 0 | 1 | 1 | 0 | 2026-09-28T22:01:02.852585+08:00 | 2026-09-28T22:03:50.989202+08:00 |
| `run_1790610885_4ac12917` | 108 | 0 | 2 | 2 | 0 | 2026-09-28T23:55:57.558587+08:00 | 2026-09-29T00:15:00.995342+08:00 |
| `run_1790613054_c2347195` | 223 | 0 | 4 | 4 | 0 | 2026-09-29T00:32:12.886435+08:00 | 2026-09-29T01:17:56.214809+08:00 |
| `run_1790682875_320744e6` | 44 | 0 | 2 | 2 | 0 | 2026-09-29T19:57:19.985842+08:00 | 2026-09-29T20:06:08.062942+08:00 |
| `run_1790683801_f424f8ca` | 54 | 0 | 2 | 2 | 0 | 2026-09-29T20:11:29.812373+08:00 | 2026-09-29T20:24:36.357858+08:00 |
| `run_1790685523_5ca8bd43` | 102 | 0 | 3 | 3 | 0 | 2026-09-29T20:40:09.765979+08:00 | 2026-09-29T21:10:26.802648+08:00 |
| `run_1790687473_27238eae` | 22 | 0 | 2 | 2 | 0 | 2026-09-29T21:12:28.948312+08:00 | 2026-09-29T21:16:53.598674+08:00 |
| `run_1790691706_7b5e4c0c` | 43 | 0 | 2 | 2 | 0 | 2026-09-29T22:23:07.296310+08:00 | 2026-09-29T22:32:48.124332+08:00 |
| `run_1790694365_fabfd10c` | 77 | 0 | 2 | 2 | 0 | 2026-09-29T23:07:29.822919+08:00 | 2026-09-29T23:30:00.097520+08:00 |
| `run_1790698326_fae4c648` | 241 | 0 | 5 | 5 | 0 | 2026-09-30T00:13:00.606436+08:00 | 2026-09-30T01:15:01.497148+08:00 |
| `run_1790702183_23f834fe` | 132 | 0 | 2 | 2 | 0 | 2026-09-30T01:17:43.372223+08:00 | 2026-09-30T01:45:00.926354+08:00 |
| `run_1790769363_931f25e7` | 114 | 0 | 3 | 3 | 0 | 2026-09-30T19:58:00.119680+08:00 | 2026-09-30T20:30:01.405192+08:00 |
| `run_1790771925_53175675` | 33 | 0 | 2 | 2 | 0 | 2026-09-30T20:43:00.005867+08:00 | 2026-09-30T20:58:00.312965+08:00 |
| `run_1790773140_ffb577f1` | 16 | 0 | 1 | 1 | 0 | 2026-09-30T21:00:21.019106+08:00 | 2026-09-30T21:02:45.716157+08:00 |
| `run_1790782164_423b6680` | 276 | 0 | 2 | 2 | 0 | 2026-09-30T23:30:41.347908+08:00 | 2026-10-01T00:00:01.259871+08:00 |
| `run_1790812536_1e581fa6` | 239 | 0 | 4 | 4 | 0 | 2026-10-01T07:57:09.566935+08:00 | 2026-10-01T08:30:51.204196+08:00 |
| `run_1790814686_56198f46` | 8 | 0 | 1 | 1 | 0 | 2026-10-01T08:32:22.584303+08:00 | 2026-10-01T08:33:19.490915+08:00 |
| `run_1790819138_953e91a0` | 567 | 0 | 5 | 5 | 0 | 2026-10-01T09:46:23.456358+08:00 | 2026-10-01T10:45:27.382853+08:00 |
| `run_1790822848_9dba9d16` | 14 | 0 | 1 | 1 | 0 | 2026-10-01T10:48:02.685818+08:00 | 2026-10-01T10:49:34.975263+08:00 |
| `run_1790822893_8b6765dc` | 4 | 0 | 1 | 1 | 0 | 2026-10-01T10:48:47.935145+08:00 | 2026-10-01T10:49:36.372163+08:00 |
| `run_1790853042_273606e6` | 263 | 0 | 4 | 4 | 0 | 2026-10-01T19:11:56.432013+08:00 | 2026-10-01T19:45:34.720459+08:00 |
| `run_1790866560_e0db5094` | 51 | 0 | 2 | 2 | 0 | 2026-10-01T22:57:15.401194+08:00 | 2026-10-01T23:01:53.248621+08:00 |
| `run_1790866950_2fa12231` | 5 | 0 | 1 | 1 | 0 | 2026-10-01T23:04:09.697513+08:00 | 2026-10-01T23:04:21.276944+08:00 |
| `run_1790867109_83a41445` | 11 | 0 | 1 | 1 | 0 | 2026-10-01T23:06:21.259988+08:00 | 2026-10-01T23:06:51.866705+08:00 |
| `run_1790868271_608b3617` | 212 | 0 | 3 | 3 | 0 | 2026-10-01T23:25:37.757764+08:00 | 2026-10-01T23:48:28.989418+08:00 |
| `run_1790869750_9f7675db` | 146 | 0 | 2 | 2 | 0 | 2026-10-01T23:50:17.841684+08:00 | 2026-10-02T00:11:34.828484+08:00 |
| `run_1790871136_3c83e4b1` | 51 | 0 | 2 | 2 | 0 | 2026-10-02T00:13:14.788962+08:00 | 2026-10-02T00:16:31.195019+08:00 |
| `run_1790872358_db2416ea` | 65 | 0 | 1 | 1 | 0 | 2026-10-02T00:33:47.759794+08:00 | 2026-10-02T00:39:03.372146+08:00 |
| `run_1790895625_aca01cc6` | 11 | 0 | 1 | 1 | 0 | 2026-10-02T07:01:34.731833+08:00 | 2026-10-02T07:04:37.886279+08:00 |
| `run_1790896037_31a502df` | 4 | 0 | 1 | 1 | 0 | 2026-10-02T07:08:31.314797+08:00 | 2026-10-02T07:09:02.425989+08:00 |
| `run_1790896184_74d17a0a` | 19 | 0 | 1 | 1 | 0 | 2026-10-02T07:10:58.528748+08:00 | 2026-10-02T07:14:00.081602+08:00 |
| `run_1790896544_2e39cf27` | 8 | 0 | 1 | 1 | 0 | 2026-10-02T07:16:51.214874+08:00 | 2026-10-02T07:19:31.540517+08:00 |
| `run_1790898762_a9253893` | 302 | 0 | 3 | 3 | 0 | 2026-10-02T07:53:39.165595+08:00 | 2026-10-02T08:25:07.515584+08:00 |
| `run_1790900905_db3b1924` | 154 | 0 | 2 | 2 | 0 | 2026-10-02T08:29:22.380432+08:00 | 2026-10-02T08:43:32.544151+08:00 |
| `run_1790901853_ee68b9b1` | 459 | 0 | 4 | 4 | 0 | 2026-10-02T08:45:08.305893+08:00 | 2026-10-02T09:37:46.257795+08:00 |
| `run_1790905217_6150bea0` | 459 | 0 | 5 | 5 | 0 | 2026-10-02T09:41:18.418854+08:00 | 2026-10-02T10:40:14.347352+08:00 |
| `run_1790908850_85f05712` | 305 | 0 | 3 | 3 | 0 | 2026-10-02T10:41:53.702368+08:00 | 2026-10-02T11:10:49.113429+08:00 |
| `run_1790910697_b26a4ec9` | 543 | 0 | 5 | 5 | 0 | 2026-10-02T11:12:35.384991+08:00 | 2026-10-02T12:11:29.548322+08:00 |
| `run_1790914766_b742241c` | 306 | 0 | 3 | 3 | 0 | 2026-10-02T12:20:24.308068+08:00 | 2026-10-02T12:53:55.860067+08:00 |
| `run_1790916961_19aa85d4` | 207 | 0 | 2 | 2 | 0 | 2026-10-02T12:57:00.411594+08:00 | 2026-10-02T13:13:12.478312+08:00 |
| `run_1790918339_4cb20aed` | 381 | 0 | 4 | 4 | 0 | 2026-10-02T13:20:02.851292+08:00 | 2026-10-02T14:09:53.356828+08:00 |
| `run_1790921485_c04d00f2` | 360 | 0 | 3 | 3 | 0 | 2026-10-02T14:12:24.202266+08:00 | 2026-10-02T14:42:56.909030+08:00 |
| `run_1790923481_02fa856e` | 263 | 0 | 2 | 2 | 0 | 2026-10-02T14:45:42.826432+08:00 | 2026-10-02T15:09:01.284870+08:00 |
| `run_1790925152_17848ffd` | 499 | 0 | 6 | 6 | 0 | 2026-10-02T15:13:25.046634+08:00 | 2026-10-02T16:15:20.981966+08:00 |
| `run_1790928993_57dcb6f0` | 239 | 0 | 2 | 2 | 0 | 2026-10-02T16:17:47.149552+08:00 | 2026-10-02T16:42:30.734457+08:00 |
| `run_1790930641_8ac67996` | 216 | 0 | 3 | 3 | 0 | 2026-10-02T16:44:56.034826+08:00 | 2026-10-02T17:12:15.894229+08:00 |
| `run_1790932452_c896cb00` | 483 | 0 | 4 | 4 | 0 | 2026-10-02T17:15:04.894020+08:00 | 2026-10-02T18:11:23.756002+08:00 |
| `run_1790936045_7ccb7e5b` | 250 | 0 | 3 | 3 | 0 | 2026-10-02T18:14:59.629858+08:00 | 2026-10-02T18:42:38.042900+08:00 |
| `run_1790937836_3379f6c9` | 355 | 0 | 5 | 5 | 0 | 2026-10-02T18:44:52.364393+08:00 | 2026-10-02T19:30:21.936610+08:00 |
| `run_1790956181_f5652ab9` | 465 | 318 | 1 | 1 | 0 | 2026-10-02T23:50:34.474002+08:00 | 2026-10-02T23:59:30.651455+08:00 |
| `run_1790956807_38dc01d0` | 33 | 1 | 1 | 0 | 1 | 2026-10-03T00:01:09.195629+08:00 | 2026-10-03T00:03:09.328375+08:00 |
| `run_1790958041_06d028f8` | 428 | 263 | 2 | 0 | 2 | 2026-10-03T00:21:39.905326+08:00 | 2026-10-03T00:30:21.785269+08:00 |
| `run_1790960589_1b970656` | 710 | 475 | 2 | 0 | 2 | 2026-10-03T01:04:10.920785+08:00 | 2026-10-03T01:24:27.309308+08:00 |
| `run_1790961966_53dd81b8` | 5 | 2 | 1 | 0 | 1 | 2026-10-03T01:27:00.828013+08:00 | 2026-10-03T01:27:02.177704+08:00 |
| `run_1790962329_41f8ebe8` | 797 | 571 | 2 | 0 | 2 | 2026-10-03T01:33:06.206929+08:00 | 2026-10-03T01:53:30.052689+08:00 |
| `run_1790963837_f6fc435d` | 1 | 0 | 1 | 0 | 1 | 2026-10-03T01:58:08.064973+08:00 | 2026-10-03T01:58:08.064973+08:00 |
| `run_1790964229_ae4dba0a` | 1903 | 1558 | 4 | 0 | 4 | 2026-10-03T02:04:47.233479+08:00 | 2026-10-03T02:45:24.333693+08:00 |
| `run_1790985778_95f5cc02` | 2768 | 2412 | 5 | 0 | 5 | 2026-10-03T08:04:03.511346+08:00 | 2026-10-03T09:02:59.365355+08:00 |
| `run_1790989410_9d4d1a20` | 392 | 345 | 1 | 0 | 1 | 2026-10-03T09:04:36.250487+08:00 | 2026-10-03T09:14:55.163468+08:00 |
| `run_1790990130_9fddffb2` | 1884 | 1668 | 3 | 0 | 3 | 2026-10-03T09:16:43.691993+08:00 | 2026-10-03T09:59:15.640694+08:00 |
| `run_1790992785_950a31bb` | 2938 | 2337 | 4 | 0 | 4 | 2026-10-03T10:00:47.741073+08:00 | 2026-10-03T10:59:45.388372+08:00 |
| `run_1790996415_f9ff5e78` | 2560 | 2283 | 5 | 0 | 5 | 2026-10-03T11:01:20.313280+08:00 | 2026-10-03T12:00:17.668041+08:00 |
| `run_1791000045_df1adc92` | 2725 | 2416 | 5 | 0 | 5 | 2026-10-03T12:01:50.830483+08:00 | 2026-10-03T13:00:45.775574+08:00 |
| `run_1791003676_3d58af66` | 2768 | 2404 | 5 | 0 | 5 | 2026-10-03T13:02:21.426171+08:00 | 2026-10-03T14:01:16.145135+08:00 |
| `run_1791007306_65a8de4a` | 343 | 300 | 1 | 0 | 1 | 2026-10-03T14:02:51.216068+08:00 | 2026-10-03T14:14:39.777852+08:00 |
| `run_1791008112_a50d08a3` | 910 | 779 | 2 | 0 | 2 | 2026-10-03T14:16:17.380254+08:00 | 2026-10-03T14:43:04.471545+08:00 |
| `run_1791009819_4f16006f` | 2403 | 2081 | 5 | 0 | 5 | 2026-10-03T14:44:36.916496+08:00 | 2026-10-03T15:43:45.660872+08:00 |
| `run_1791013463_c1370a2f` | 507 | 382 | 2 | 0 | 2 | 2026-10-03T15:45:19.793827+08:00 | 2026-10-03T16:00:21.835079+08:00 |
| `run_1791014641_12c10290` | 2592 | 2284 | 5 | 0 | 5 | 2026-10-03T16:05:08.113359+08:00 | 2026-10-03T17:04:03.592934+08:00 |
| `run_1791018273_c80d24ec` | 2254 | 1989 | 4 | 0 | 4 | 2026-10-03T17:05:39.313209+08:00 | 2026-10-03T17:59:26.647313+08:00 |
| `run_1791021599_d199e9dd` | 1050 | 944 | 2 | 0 | 2 | 2026-10-03T18:01:05.611796+08:00 | 2026-10-03T18:29:30.079352+08:00 |
| `run_1791023404_d05b32b1` | 1763 | 1563 | 3 | 0 | 3 | 2026-10-03T18:31:11.664113+08:00 | 2026-10-03T19:14:34.959826+08:00 |
| `run_1791026106_c815ceee` | 2591 | 2363 | 5 | 0 | 5 | 2026-10-03T19:16:14.687576+08:00 | 2026-10-03T20:15:08.186505+08:00 |
| `run_1791029737_3db8ca59` | 2587 | 2228 | 5 | 0 | 5 | 2026-10-03T20:16:47.774791+08:00 | 2026-10-03T21:15:38.373480+08:00 |
| `run_1791033368_63fa64b7` | 2726 | 2353 | 5 | 0 | 5 | 2026-10-03T21:17:19.672810+08:00 | 2026-10-03T22:16:09.551155+08:00 |
| `run_1791036999_39eed4e1` | 2487 | 2171 | 5 | 0 | 5 | 2026-10-03T22:17:40.783840+08:00 | 2026-10-03T23:16:40.368839+08:00 |
| `run_1791040631_6928e547` | 94 | 74 | 1 | 0 | 1 | 2026-10-03T23:18:15.375846+08:00 | 2026-10-03T23:21:26.484042+08:00 |
| `run_1791040992_ee674ec8` | 66 | 52 | 1 | 0 | 1 | 2026-10-03T23:24:14.150334+08:00 | 2026-10-03T23:25:47.708793+08:00 |
| `run_1791041249_fc1dd8e0` | 2730 | 2348 | 5 | 0 | 5 | 2026-10-03T23:28:28.284936+08:00 | 2026-10-04T00:27:27.296348+08:00 |
| `run_1791044880_acbd0786` | 2800 | 2469 | 5 | 0 | 5 | 2026-10-04T00:28:59.254871+08:00 | 2026-10-04T01:28:01.036159+08:00 |
| `run_1791048511_365b424d` | 2560 | 2176 | 5 | 0 | 5 | 2026-10-04T01:29:29.625253+08:00 | 2026-10-04T02:28:33.357996+08:00 |
| `run_1791052141_fe30de47` | 1226 | 1098 | 2 | 0 | 2 | 2026-10-04T02:30:01.599176+08:00 | 2026-10-04T02:56:08.588722+08:00 |
| `run_1791053867_a36e4c67` | 2722 | 2353 | 5 | 0 | 5 | 2026-10-04T02:58:43.468218+08:00 | 2026-10-04T03:57:47.568603+08:00 |
| `run_1791057497_bb44b528` | 2712 | 2367 | 5 | 0 | 5 | 2026-10-04T03:59:15.023652+08:00 | 2026-10-04T04:58:17.712429+08:00 |
| `run_1791061128_6012b10b` | 2792 | 2436 | 5 | 0 | 5 | 2026-10-04T04:59:50.309340+08:00 | 2026-10-04T05:58:48.185585+08:00 |
| `run_1791064759_671ece47` | 1422 | 1216 | 3 | 0 | 3 | 2026-10-04T06:00:18.095150+08:00 | 2026-10-04T06:30:56.536094+08:00 |
| `run_1791066780_900ee62c` | 8 | 3 | 1 | 0 | 1 | 2026-10-04T06:34:08.792296+08:00 | 2026-10-04T06:34:53.292367+08:00 |
| `run_1791067090_248ee83c` | 8 | 5 | 1 | 0 | 1 | 2026-10-04T06:39:18.272117+08:00 | 2026-10-04T06:39:48.775120+08:00 |
| `run_1791067336_5b19463d` | 1082 | 919 | 3 | 0 | 3 | 2026-10-04T06:43:16.521032+08:00 | 2026-10-04T07:14:13.351790+08:00 |
| `run_1791069297_2bc86051` | 501 | 460 | 1 | 0 | 1 | 2026-10-04T07:16:02.557490+08:00 | 2026-10-04T07:28:01.309194+08:00 |
| `run_1791070109_cc475056` | 1234 | 1072 | 3 | 0 | 3 | 2026-10-04T07:29:27.257157+08:00 | 2026-10-04T07:57:43.920183+08:00 |
| `run_1791071895_523f85aa` | 228 | 124 | 2 | 0 | 2 | 2026-10-04T07:59:15.769978+08:00 | 2026-10-04T08:12:28.759286+08:00 |
| `run_1791072787_b44264cd` | 2553 | 2146 | 5 | 0 | 5 | 2026-10-04T08:14:09.477731+08:00 | 2026-10-04T09:13:00.535753+08:00 |
| `run_1791076424_50193a93` | 2847 | 2509 | 5 | 0 | 5 | 2026-10-04T09:14:38.994054+08:00 | 2026-10-04T10:13:44.998849+08:00 |
| `run_1791080054_04834687` | 660 | 556 | 2 | 0 | 2 | 2026-10-04T10:15:13.137364+08:00 | 2026-10-04T10:30:20.908857+08:00 |
| `run_1791081118_a7cf46b9` | 2084 | 1790 | 4 | 0 | 4 | 2026-10-04T10:33:05.711167+08:00 | 2026-10-04T11:18:16.898582+08:00 |
| `run_1791084018_a2993851` | 14 | 9 | 1 | 0 | 1 | 2026-10-04T11:21:26.809093+08:00 | 2026-10-04T11:22:18.914292+08:00 |
| `run_1791084345_41679360` | 2560 | 2233 | 5 | 0 | 5 | 2026-10-04T11:26:52.498789+08:00 | 2026-10-04T12:25:45.746639+08:00 |
| `run_1791087977_c480f2fa` | 640 | 525 | 2 | 0 | 2 | 2026-10-04T12:27:22.998631+08:00 | 2026-10-04T12:40:12.863222+08:00 |
| `run_1791088949_8a69164e` | 2697 | 2379 | 5 | 0 | 5 | 2026-10-04T12:43:31.244880+08:00 | 2026-10-04T13:42:30.065558+08:00 |
| `run_1791092582_c6d8b67f` | 1695 | 1470 | 4 | 0 | 4 | 2026-10-04T13:44:06.278684+08:00 | 2026-10-04T14:27:00.113219+08:00 |
| `run_1791095281_4743bbb1` | 2725 | 2394 | 5 | 0 | 5 | 2026-10-04T14:29:02.393458+08:00 | 2026-10-04T15:28:02.271619+08:00 |
| `run_1791098911_ca8eae61` | 1591 | 1354 | 4 | 0 | 4 | 2026-10-04T15:29:30.727577+08:00 | 2026-10-04T16:10:11.179977+08:00 |
| `run_1791101455_05564309` | 2661 | 2343 | 5 | 0 | 5 | 2026-10-04T16:12:01.584676+08:00 | 2026-10-04T17:10:55.876855+08:00 |
| `run_1791105086_1e64e0f8` | 1353 | 1142 | 3 | 0 | 3 | 2026-10-04T17:12:35.955270+08:00 | 2026-10-04T17:44:55.075075+08:00 |
| `run_1791107133_3f0342b8` | 2385 | 2020 | 5 | 0 | 5 | 2026-10-04T17:46:46.481553+08:00 | 2026-10-04T18:45:35.025821+08:00 |
| `run_1791110764_61feccf3` | 1089 | 1000 | 2 | 0 | 2 | 2026-10-04T18:47:16.574196+08:00 | 2026-10-04T19:14:12.302043+08:00 |
| `run_1791112480_51b3a76a` | 2699 | 2414 | 4 | 0 | 4 | 2026-10-04T19:15:49.016247+08:00 | 2026-10-04T20:14:39.000747+08:00 |
| `run_1791116112_9db06308` | 2529 | 2142 | 4 | 0 | 4 | 2026-10-04T20:16:30.203663+08:00 | 2026-10-04T21:13:00.621632+08:00 |
| `run_1791119628_cdb7d7c2` | 2746 | 2378 | 5 | 0 | 5 | 2026-10-04T21:14:52.884950+08:00 | 2026-10-04T22:13:48.652696+08:00 |
| `run_1791123262_fe49178c` | 2458 | 2130 | 4 | 0 | 4 | 2026-10-04T22:15:34.033267+08:00 | 2026-10-04T23:14:23.111657+08:00 |
| `run_1791126894_6f34a6a9` | 391 | 304 | 1 | 0 | 1 | 2026-10-04T23:16:10.603518+08:00 | 2026-10-04T23:28:36.210946+08:00 |
| `run_1791127749_bd53b9e8` | 2567 | 2206 | 4 | 2 | 2 | 2026-10-04T23:30:14.110046+08:00 | 2026-10-05T00:29:09.800554+08:00 |
| `run_1791131381_baae0ef5` | 2659 | 2371 | 4 | 4 | 0 | 2026-10-05T00:30:53.538626+08:00 | 2026-10-05T01:29:39.501258+08:00 |
| `run_1791135014_15577a71` | 2350 | 2072 | 4 | 4 | 0 | 2026-10-05T01:31:29.615825+08:00 | 2026-10-05T02:30:00.228497+08:00 |
| `run_1791138648_00dd5e67` | 886 | 884 | 2 | 2 | 0 | 2026-10-05T02:31:51.994779+08:00 | 2026-10-05T02:56:36.210716+08:00 |
| `run_1791140353_83060865` | 1579 | 1576 | 3 | 3 | 0 | 2026-10-05T03:00:16.206741+08:00 | 2026-10-05T03:41:23.876335+08:00 |
| `run_1791142981_3d2f57d1` | 2320 | 2316 | 5 | 5 | 0 | 2026-10-05T03:44:15.220880+08:00 | 2026-10-05T04:43:02.258840+08:00 |
| `run_1791146614_c03299a7` | 2182 | 2177 | 5 | 5 | 0 | 2026-10-05T04:44:39.487529+08:00 | 2026-10-05T05:43:33.559138+08:00 |
| `run_1791150245_90251ae4` | 1040 | 1037 | 3 | 3 | 0 | 2026-10-05T05:45:09.532105+08:00 | 2026-10-05T06:22:57.033152+08:00 |
| `run_1791152702_e68ca564` | 17 | 15 | 1 | 1 | 0 | 2026-10-05T06:26:14.913609+08:00 | 2026-10-05T06:30:00.112837+08:00 |
| `run_1791154036_1dffb3b0` | 1 | 0 | 0 | 0 | 0 | 2026-10-05T06:49:35.119360+08:00 | 2026-10-05T06:49:35.119360+08:00 |
| `run_1791154261_b189fd72` | 283 | 282 | 1 | 1 | 0 | 2026-10-05T06:52:40.040752+08:00 | 2026-10-05T06:58:38.565289+08:00 |

## 2. Payload schema compatibility

Payload versions explicitly named `research_schema_version`, `prediction_schema_version`, `lifecycle_schema_version`, and `trace_schema_version`: **absent** in both cohorts. Actual prediction payloads use the same event type and key signature across the compared sample (1 weekday signature(s), 1 weekend signature(s); identical=True). Both cohorts expose the same key names for core fields; non-null availability is below (numerators/denominators are snapshot rows, not markets):

| Payload field | Weekday present | Weekend present |
|---|---:|---:|
| `snapshot_ts` | 9767/9767 | 72228/72228 |
| `market_slug` | 9767/9767 | 72228/72228 |
| `time_left_sec` | 9767/9767 | 72228/72228 |
| `joint_fresh` | 9767/9767 | 72228/72228 |
| `settlement_state_side` | 9767/9767 | 72228/72228 |
| `required_move_sigma` | 9632/9767 | 71113/72228 |
| `required_move_bps` | 9669/9767 | 71474/72228 |
| `p_up_ex_market` | 9667/9767 | 71408/72228 |
| `market_mid_up` | 4748/9767 | 34464/72228 |
| `market_mid_down` | 4848/9767 | 35039/72228 |
| `market_mid_up_fresh` | 9767/9767 | 72228/72228 |
| `market_mid_down_fresh` | 9767/9767 | 72228/72228 |
| `market_quote_up_fresh` | 9767/9767 | 72228/72228 |
| `market_quote_down_fresh` | 9767/9767 | 72228/72228 |
| `p_ex_fresh` | 9767/9767 | 72228/72228 |
| `sigma_ex_market_fresh` | 9767/9767 | 72228/72228 |

Probability model version values weekday: `{'existing_twap_average_approx_v1': 9767}`; weekend: `{'existing_twap_average_approx_v1': 72228}`. Canonical settlement summary fields are stored under `settlement_side`, `settlement_reference_is_canonical`, `settlement_reference_source`, and `summary_ts` (not all under the output alias `canonical_settlement_side`). Their availability is 21/21 weekday and 144/144 weekend summaries.

**Core comparison schema comparability: CONFIRMED_BY_PAYLOAD.** Weekday and weekend prediction payloads have the same complete key signature, matching probability-model version, and matching availability for the core comparison fields; settlement summaries expose the same canonical source/side fields in both cohorts. Explicit research/prediction/lifecycle/trace schema version tags are absent, so those version labels themselves cannot be compared. This confirms empirical payload compatibility for the compared core fields, not provenance of their upstream production.

## 3. Monday market completeness

Snapshot created at 2026-10-05T06:58:50.605285+08:00. Snapshot time range for Monday starts at 2026-10-05T00:00:00+08:00; completed 15-minute slots enumerated through the last slot whose market had ended by snapshot time. This is 27 expected completed slots plus 1 active/unsettled slot(s), not simply 24 slots. Status counts: {'SYNCHRONIZED_USABLE': 18, 'MISSING_MARKET': 6, 'INTERRUPTED': 3, 'UNSETTLED_AT_SNAPSHOT': 1}. The full slot listing:

| Taipei start | Market | Status | Prediction rows | Settlement summary |
|---|---|---|---:|---|
| 2026-10-05T00:00:00+08:00 | `btc-updown-15m-1791129600` | SYNCHRONIZED_USABLE | 562 | True |
| 2026-10-05T00:15:00+08:00 | `btc-updown-15m-1791130500` | MISSING_MARKET | 0 | False |
| 2026-10-05T00:30:00+08:00 | `btc-updown-15m-1791131400` | SYNCHRONIZED_USABLE | 554 | True |
| 2026-10-05T00:45:00+08:00 | `btc-updown-15m-1791132300` | SYNCHRONIZED_USABLE | 672 | True |
| 2026-10-05T01:00:00+08:00 | `btc-updown-15m-1791133200` | SYNCHRONIZED_USABLE | 530 | True |
| 2026-10-05T01:15:00+08:00 | `btc-updown-15m-1791134100` | MISSING_MARKET | 0 | False |
| 2026-10-05T01:30:00+08:00 | `btc-updown-15m-1791135000` | SYNCHRONIZED_USABLE | 589 | True |
| 2026-10-05T01:45:00+08:00 | `btc-updown-15m-1791135900` | SYNCHRONIZED_USABLE | 543 | True |
| 2026-10-05T02:00:00+08:00 | `btc-updown-15m-1791136800` | SYNCHRONIZED_USABLE | 549 | True |
| 2026-10-05T02:15:00+08:00 | `btc-updown-15m-1791137700` | SYNCHRONIZED_USABLE | 391 | True |
| 2026-10-05T02:30:00+08:00 | `btc-updown-15m-1791138600` | SYNCHRONIZED_USABLE | 487 | True |
| 2026-10-05T02:45:00+08:00 | `btc-updown-15m-1791139500` | MISSING_MARKET | 0 | False |
| 2026-10-05T03:00:00+08:00 | `btc-updown-15m-1791140400` | SYNCHRONIZED_USABLE | 585 | True |
| 2026-10-05T03:15:00+08:00 | `btc-updown-15m-1791141300` | SYNCHRONIZED_USABLE | 565 | True |
| 2026-10-05T03:30:00+08:00 | `btc-updown-15m-1791142200` | INTERRUPTED | 21 | True |
| 2026-10-05T03:45:00+08:00 | `btc-updown-15m-1791143100` | SYNCHRONIZED_USABLE | 545 | True |
| 2026-10-05T04:00:00+08:00 | `btc-updown-15m-1791144000` | SYNCHRONIZED_USABLE | 659 | True |
| 2026-10-05T04:15:00+08:00 | `btc-updown-15m-1791144900` | MISSING_MARKET | 0 | False |
| 2026-10-05T04:30:00+08:00 | `btc-updown-15m-1791145800` | INTERRUPTED | 5 | True |
| 2026-10-05T04:45:00+08:00 | `btc-updown-15m-1791146700` | SYNCHRONIZED_USABLE | 630 | True |
| 2026-10-05T05:00:00+08:00 | `btc-updown-15m-1791147600` | SYNCHRONIZED_USABLE | 570 | True |
| 2026-10-05T05:15:00+08:00 | `btc-updown-15m-1791148500` | SYNCHRONIZED_USABLE | 418 | True |
| 2026-10-05T05:30:00+08:00 | `btc-updown-15m-1791149400` | MISSING_MARKET | 0 | False |
| 2026-10-05T05:45:00+08:00 | `btc-updown-15m-1791150300` | SYNCHRONIZED_USABLE | 657 | True |
| 2026-10-05T06:00:00+08:00 | `btc-updown-15m-1791151200` | SYNCHRONIZED_USABLE | 220 | True |
| 2026-10-05T06:15:00+08:00 | `btc-updown-15m-1791152100` | INTERRUPTED | 15 | True |
| 2026-10-05T06:30:00+08:00 | `btc-updown-15m-1791153000` | MISSING_MARKET | 0 | False |
| 2026-10-05T06:45:00+08:00 | `btc-updown-15m-1791153900` | UNSETTLED_AT_SNAPSHOT | 0 | False |

The first six-hour window 00:00–05:59 contains 24 scheduled start slots: 24 completed starts, with statuses shown above. In the full snapshot window through 06:58, 21 markets have synchronized timelines, including 3 that crossed run boundaries; 6 completed grid slots have no research snapshot or settlement record, and the 06:45 market had not ended. `MISSING_MARKET` here means absent from these snapshots; without an exchange market catalog, it cannot distinguish unlisted market from collection gap.

## 4. Weekday synchronized quality

Per-market quality and checkpoint availability are in `weekday_market_quality.csv`. Snapshot count and coverage are computed from all available prediction snapshots for each slug; coverage is capped at one 900-second market interval. Multiple run IDs are listed comma-separated and flagged by `run_count`.

| Metric | Median | P10 | P25 | P75 | P90 |
|---|---:|---:|---:|---:|---:|
| coverage_ratio | 0.977 | 0.898 | 0.970 | 0.978 | 0.978 |
| largest_gap_sec | 25.500 | 11.707 | 14.356 | 57.788 | 178.038 |
| joint_fresh_pct | 53.734 | 43.429 | 45.018 | 62.253 | 79.211 |
| snapshot_count | 549.000 | 391.000 | 487.000 | 585.000 | 657.000 |

A market with a nonempty timeline is not automatically FULL/GOOD. Evaluate checkpoint availability and joint freshness per market in the CSV.

## 5. Sigma stratification

Each row below is one independent market at one checkpoint. Wilson 95% intervals are shown for every nonempty bin; repeated checkpoints across the same market are still dependent across rows.

**WEEKDAY T-300:** <0.5 sigma 1/6 (0.167, CI 0.030–0.564); 0.5-1 sigma 1/6 (0.167, CI 0.030–0.564); 1-2 sigma 0/4 (0.000, CI 0.000–0.490); 2-3 sigma 0/1 (0.000, CI 0.000–0.793); 3-5 sigma 0/0 (NA, CI NA–NA); >5 sigma 0/0 (NA, CI NA–NA)
**WEEKEND T-300:** <0.5 sigma 13/56 (0.232, CI 0.141–0.358); 0.5-1 sigma 2/40 (0.050, CI 0.014–0.165); 1-2 sigma 0/21 (0.000, CI 0.000–0.155); 2-3 sigma 0/4 (0.000, CI 0.000–0.490); 3-5 sigma 0/1 (0.000, CI 0.000–0.793); >5 sigma 0/2 (0.000, CI 0.000–0.658)
**WEEKDAY T-180:** <0.5 sigma 0/5 (0.000, CI 0.000–0.434); 0.5-1 sigma 0/4 (0.000, CI 0.000–0.490); 1-2 sigma 1/4 (0.250, CI 0.046–0.699); 2-3 sigma 0/2 (0.000, CI 0.000–0.658); 3-5 sigma 0/1 (0.000, CI 0.000–0.793); >5 sigma 0/2 (0.000, CI 0.000–0.658)
**WEEKEND T-180:** <0.5 sigma 3/36 (0.083, CI 0.029–0.218); 0.5-1 sigma 0/47 (0.000, CI 0.000–0.076); 1-2 sigma 0/25 (0.000, CI 0.000–0.133); 2-3 sigma 0/10 (0.000, CI 0.000–0.278); 3-5 sigma 0/1 (0.000, CI 0.000–0.793); >5 sigma 0/3 (0.000, CI 0.000–0.561)
**WEEKDAY T-120:** <0.5 sigma 0/4 (0.000, CI 0.000–0.490); 0.5-1 sigma 0/2 (0.000, CI 0.000–0.658); 1-2 sigma 1/4 (0.250, CI 0.046–0.699); 2-3 sigma 1/4 (0.250, CI 0.046–0.699); 3-5 sigma 0/1 (0.000, CI 0.000–0.793); >5 sigma 0/3 (0.000, CI 0.000–0.561)
**WEEKEND T-120:** <0.5 sigma 3/29 (0.103, CI 0.036–0.264); 0.5-1 sigma 1/39 (0.026, CI 0.005–0.132); 1-2 sigma 0/33 (0.000, CI 0.000–0.104); 2-3 sigma 0/14 (0.000, CI 0.000–0.215); 3-5 sigma 0/5 (0.000, CI 0.000–0.434); >5 sigma 0/3 (0.000, CI 0.000–0.561)
**WEEKDAY T-60:** <0.5 sigma 0/2 (0.000, CI 0.000–0.658); 0.5-1 sigma 1/2 (0.500, CI 0.095–0.905); 1-2 sigma 0/1 (0.000, CI 0.000–0.793); 2-3 sigma 0/0 (NA, CI NA–NA); 3-5 sigma 0/3 (0.000, CI 0.000–0.561); >5 sigma 0/1 (0.000, CI 0.000–0.793)
**WEEKEND T-60:** <0.5 sigma 1/14 (0.071, CI 0.013–0.315); 0.5-1 sigma 1/11 (0.091, CI 0.016–0.377); 1-2 sigma 0/12 (0.000, CI 0.000–0.242); 2-3 sigma 0/9 (0.000, CI 0.000–0.299); 3-5 sigma 0/8 (0.000, CI 0.000–0.324); >5 sigma 0/3 (0.000, CI 0.000–0.561)

Strict conclusion: **INSUFFICIENT_N** for the weekday relationship. Most weekday bins have fewer than 10 markets; zero flips in those bins are not evidence of zero risk. Do not label this “MIXED” as if a stable opposing pattern had been measured.

## 6. Entry sample independence

The existing entry join reads `SHADOW_SIM_SETTLED` events and the accounting helper deduplicates by `simulation_id` (latest event wins). The synchronized entry table has at most one settled shadow entry per market in each regime; the output reports row and market counts, wins/losses, gross PnL and average PnL. There is therefore no need to invent a per-market selection rule. Entry-level N equals unique-market N for these records. These are settled shadow entries, not actual live fills. See `entry_independence.csv`.

## 7. Repricing event definition

Repo definition: find a fresh synchronized 30-second mid move over 27–33 seconds, require absolute move ≥10c, then merge consecutive same-direction events when their start times are at most 30 seconds apart, retaining the largest move in the group. This suppresses many adjacent-baseline duplicates, but different groups can remain serially dependent; overlapping event intervals are counted below. **EVENT COUNT IS NOT AN INDEPENDENT SAMPLE.**

- WEEKDAY: 512 raw qualifying windows → 75 coalesced events; 16/21 markets (0.762) had ≥1; median 4.0 and P90 7.0 coalesced events per affected market; overlapping event pairs=9.
- ALL_WEEKEND: 3385 raw qualifying windows → 442 coalesced events; 117/144 markets (0.812) had ≥1; median 3 and P90 7.0 coalesced events per affected market; overlapping event pairs=51.
- TIME_MATCHED_WEEKEND: 1205 raw qualifying windows → 123 coalesced events; 27/31 markets (0.871) had ≥1; median 4 and P90 7.0 coalesced events per affected market; overlapping event pairs=17.

The market-level binary metric is the affected-market fraction above. Event totals are descriptive only.

## 8. Market vs analytic calibration

Wilson intervals apply to observed flip rates. Brier difference is analytic minus market; negative favors analytic for that checkpoint. N is independent markets within the checkpoint.

| Regime | Checkpoint | N | Flips | Rate [Wilson 95% CI] | Mean market p | Mean analytic p | Market Brier | Analytic Brier | Analytic−market |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| WEEKDAY | T-300 | 17 | 2 | 0.118 [0.033, 0.343] | 0.179 | 0.251 | 0.130 | 0.127 | -0.004 |
| WEEKEND | T-300 | 124 | 15 | 0.121 [0.075, 0.190] | 0.176 | 0.257 | 0.092 | 0.113 | 0.021 |
| TIME_MATCHED_WEEKEND | T-300 | 26 | 4 | 0.154 [0.062, 0.335] | 0.210 | 0.252 | 0.100 | 0.100 | -0.000 |
| WEEKDAY | T-180 | 18 | 1 | 0.056 [0.010, 0.258] | 0.155 | 0.185 | 0.050 | 0.105 | 0.055 |
| WEEKEND | T-180 | 125 | 4 | 0.032 [0.013, 0.079] | 0.115 | 0.208 | 0.035 | 0.069 | 0.034 |
| TIME_MATCHED_WEEKEND | T-180 | 25 | 0 | 0.000 [0.000, 0.133] | 0.071 | 0.175 | 0.009 | 0.050 | 0.041 |
| WEEKDAY | T-120 | 18 | 2 | 0.111 [0.031, 0.328] | 0.111 | 0.112 | 0.031 | 0.143 | 0.111 |
| WEEKEND | T-120 | 124 | 4 | 0.032 [0.013, 0.080] | 0.098 | 0.168 | 0.031 | 0.063 | 0.032 |
| TIME_MATCHED_WEEKEND | T-120 | 24 | 1 | 0.042 [0.007, 0.202] | 0.083 | 0.147 | 0.012 | 0.059 | 0.046 |
| WEEKDAY | T-60 | 18 | 2 | 0.111 [0.031, 0.328] | 0.022 | 0.117 | 0.102 | 0.083 | -0.019 |
| WEEKEND | T-60 | 122 | 2 | 0.016 [0.005, 0.058] | 0.070 | 0.105 | 0.020 | 0.036 | 0.016 |
| TIME_MATCHED_WEEKEND | T-60 | 25 | 0 | 0.000 [0.000, 0.133] | 0.075 | 0.083 | 0.036 | 0.022 | -0.014 |
| WEEKDAY | T-30 | 18 | 1 | 0.056 [0.010, 0.258] | 0.117 | 0.065 | 0.001 | 0.004 | 0.004 |
| WEEKEND | T-30 | 136 | 3 | 0.022 [0.008, 0.063] | 0.052 | 0.042 | 0.019 | 0.011 | -0.008 |
| TIME_MATCHED_WEEKEND | T-30 | 29 | 0 | 0.000 [0.000, 0.117] | 0.063 | 0.014 | 0.037 | 0.002 | -0.035 |

Early weekday replication of “market calibrates better”: **INSUFFICIENT_N**. Weekday Brier ranking varies by checkpoint (at T-300 analytic is slightly lower; at T-180/T-120 market is lower); do not decide by sign alone.

## 9. Time-of-day matching

Exact Taipei-hour distributions: weekday `{0: 3, 1: 3, 2: 3, 3: 4, 4: 3, 5: 3, 6: 2}`; matched weekend `{0: 5, 1: 6, 2: 6, 3: 4, 4: 3, 5: 3, 6: 4}`. The match is hour-of-day 00–06 inclusive, drawn from Saturday and Sunday; it is not a matched weekday/session date. T-300 comparison:

| Cohort | N | Flip rate [Wilson CI] | Market Brier | Analytic Brier | Median required_move_sigma |
|---|---:|---:|---:|---:|---:|
| WEEKDAY | 17 | 0.118 [0.033, 0.343] | 0.130 | 0.127 | 0.607 |
| WEEKEND | 124 | 0.121 [0.075, 0.190] | 0.092 | 0.113 | 0.576 |
| TIME_MATCHED_WEEKEND | 26 | 0.154 [0.062, 0.335] | 0.100 | 0.100 | 0.577 |

*The hour-matched set contains 31 market starts; 26 have usable T-300 predictions and settlement outcomes.

The weekday and all-weekend T-300 point estimates are nearly identical (11.8% vs 12.1%); the matched-weekend point estimate is 15.4%, with a wide interval overlapping both. The small matched sample does not establish a regime effect, and hour/session composition remains a plausible confounder for other contrasts.

## 10. Supplemental checkpoints

The repository nearest-checkpoint rule is absolute distance ≤12 seconds in `time_left_sec`; this pass used that same rule on the snapshot payload rows without changing source code. Additional rows are labelled `SUPPLEMENTAL_NONCANONICAL_REPORT_ONLY` in `calibration_checkpoints.csv`. A row is measurable only if a nearby snapshot and both known settlement sides exist; probability scores remain missing where their freshness/value is absent.

## 11. Required_move_bps

The synchronized prediction payloads do contain `required_move_bps`. Supplemental bins use the existing entry-stop fixed absolute-bps bins: 0–2, 2–5, 5–10, 10–20, >20. T-300/T-180 counts, flips and rates, plus present/zero/missing counts, are in `sigma_bins.csv`; zero is explicitly counted, not imputed. These are market-level per checkpoint.

## 12. Sample-size requirement

For conservative binomial precision use worst-case p=0.5 and normal 95% half-width: n≈0.25×(1.96/h)². Required independent completed observations per regime: **97** for ±10pp, **171** for ±7.5pp, **385** for ±5pp. These target a single aggregate flip rate, not subgroup calibration.

For T-300 flip-rate comparison, the precision targets imply **97 usable T-300 markets** as the ±10pp MINIMUM and **171** as the ±7.5pp PREFERRED target (385 for ±5pp). In this Monday snapshot, 17/21 synchronized markets have usable T-300 scores (81.0%); relative to all 27 expected completed slots that is 63.0%. At four starts/hour and 77.8% observed synchronized rate, collecting 97 T-300 observations needs about 120 synchronized markets, or about **39 collection hours**; 171 observations needs about 211 synchronized markets, or **68 hours**. These are planning estimates from one short window, not a calendar-day requirement.

Sigma bins need separate planning. At weekday T-300, the 2–3σ bin is 1/17; that observed share would need about 170 usable T-300 markets to expect 10 in that bin. In the weekend reference, the same bin is 4/124 (about 3.2%), implying about 310 usable T-300 markets to expect 10. Rare 3–5σ and >5σ bins need hundreds or more and may remain underpowered. A practical MINIMUM is **120 completed synchronized weekday markets** for aggregate T-300 precision; PREFERRED is **211**. If stable 2–3σ analysis is a priority, plan for roughly **384 synchronized markets** (around 123 collection hours at the observed rate); do not interpret rarer bins until their actual N is adequate.

## 13. Corrected interpretation

**PROVENANCE MANIFEST ABSENT** is confirmed. That fact does not itself mean **DATA SCHEMA INCOMPATIBLE**. The compared core payload schema is empirically confirmed across weekday/weekend cohorts; lifecycle/trace schema version labels remain unavailable. Do not describe 404 as prediction rows: it is strategy-run registry rows and unique run IDs. Entry counts are independent markets in these snapshots. Repricing event counts remain serially dependent. The weekday sigma result is **INSUFFICIENT_N**, and no strategy conclusion follows.

| Finding | Previous wording | Corrected wording | Confidence |
|---|---|---|---|
| Run count | “404 recorded runs are legacy/unknown” | 404 strategy_runs rows and unique run IDs; 72 have prediction rows; 103836 prediction rows | High for counts |
| Payload schema | Manifest absent implied uncertain comparability | Core payload comparability CONFIRMED_BY_PAYLOAD; explicit schema version tags absent | Moderate-high for compared core fields |
| 21 synchronized markets | Count treated as quality | Per-market coverage, gap, freshness and checkpoint availability enumerated; presence alone is insufficient | High once CSV checked |
| Sigma | MIXED | INSUFFICIENT_N under bin-size rule | High for small N |
| Entry N | 14/41 described without independence check | Entry rows and distinct markets verified; canonical simulation_id dedup applied | High |
| Repricing rate | Events/market compared as rates | ≥10c events are coalesced but serially dependent; market-level YES/NO is primary | High for definition |
| Market vs p_ex | Mixed based on Brier signs | Per-checkpoint Wilson intervals and N; weekday answer INSUFFICIENT_N | Moderate |
| Provenance | Legacy unknown presented as schema limitation | Manifest absence is distinct from payload schema compatibility | Moderate |

Safety: source code unchanged; no commit/push; bot not stopped/restarted; source DBs and active Parquet not mutated; all calculations used only the existing offline snapshots.
