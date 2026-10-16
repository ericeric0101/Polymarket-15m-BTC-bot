import json
import sqlite3

import pytest

from scripts import historical_freshness_recomputation as h


def row(**updates):
    return {"snapshot_ts":100.5,"btc_source_ts":100.7,"btc_fresh":False,"btc_spot":None,
            "p_ex_fresh":True,"p_up_ex_market":.6,"p_ex_source_ts":99,
            "sigma_ex_market_fresh":True,"p_ex_age_sec":1.5,
            "market_mid_fresh":True,"market_mid_up_fresh":True,"market_mid_down_fresh":False,
            "market_mid_up":.55,"market_quote_up_received_ts":100.4,
            "market_quote_down_received_ts":100.3,
            "twap_fresh":True,"official_twap":1000,"twap_source_ts":99,"joint_fresh":False,
            **updates}


def test_exact_source_with_prior_receipt_recovers_without_remote_local_subtraction():
    evidence=h.TickEvidence([(100700,100400,50)])
    fixed,labels,reasons=h.classify_row(row(),evidence)
    assert fixed["btc_spot"]==50 and fixed["joint_fresh"]
    assert labels["BTC"]==labels["joint"]=="FRESH_RECOMPUTED"
    assert reasons["BTC"]=="LOCAL_RECEIPT_AUTHORITY+SAME_CLOCK_DOMAIN"
    assert fixed["btc_recomputed_value_age_sec"]==0


def test_future_receipt_and_later_source_do_not_affect_earlier_classification():
    earlier=h.TickEvidence([(100700,100400,50)])
    later=h.TickEvidence([(100700,100400,50),(999999,101000,1),(100700,101000,999)])
    assert h.classify_row(row(),earlier)==h.classify_row(row(),later)
    assert h.classify_row(row(),h.TickEvidence([(100700,100600,50)]))[1]["BTC"]=="NOT_RECOMPUTABLE"


def test_millisecond_receipt_upper_bound_excludes_possible_future():
    assert h.TickEvidence([(100700,100500,50)]).exact(100.7,100.5005)[1]=="NOT_RECOMPUTABLE"


def test_same_source_conflicting_prices_rejected():
    assert h.TickEvidence([(100700,100300,50),(100700,100400,51)]).exact(100.7,100.5)[1]=="NOT_RECOMPUTABLE"


def test_repeat_receipts_do_not_rejuvenate_value():
    assert h.TickEvidence([(90000,90000,50),(90000,100000,50)]).exact(90,100.5)[1]=="STALE_VALID"


def test_source_domain_value_age_stale_despite_recent_transport():
    assert h.TickEvidence([(90000,100000,50),(101000,100100,51)]).exact(90,100.5)[1]=="STALE_VALID"


def test_missing_timestamps_are_not_fresh():
    _,labels,_=h.classify_row(row(btc_source_ts=None),h.TickEvidence([]))
    assert labels["BTC"]=="MISSING_REQUIRED_FIELD"
    assert labels["joint"]=="MISSING_REQUIRED_FIELD"


def test_original_true_flag_cannot_override_missing_source_timestamp():
    _,labels,_=h.classify_row(row(btc_fresh=True,btc_spot=50,btc_source_ts=None),h.TickEvidence([]))
    assert labels["BTC"]=="MISSING_REQUIRED_FIELD"


def test_original_true_flag_does_not_restore_a_cleared_probability():
    _,labels,_=h.classify_row(row(p_up_ex_market=None),h.TickEvidence([]))
    assert labels["p_ex"]=="NOT_RECOMPUTABLE"


def test_cleared_bbo_is_not_recovered_by_recent_receipt():
    fixed,labels,_=h.classify_row(row(market_mid_fresh=False,market_mid_up=None,market_mid_up_fresh=False),h.TickEvidence([(100700,100400,50)]))
    assert labels["market"]=="NOT_RECOMPUTABLE" and fixed["joint_fresh"] is False


def test_original_strict_flags_remain_explicitly_original():
    _,labels,_=h.classify_row(row(btc_fresh=True,btc_spot=50,joint_fresh=True),h.TickEvidence([]))
    assert set(labels.values())=={"FRESH_ORIGINAL"}


def test_settlement_outcome_cannot_affect_freshness():
    a=h.classify_row(row(canonical_settlement_side="UP"),h.TickEvidence([]))[1:]
    b=h.classify_row(row(canonical_settlement_side="DOWN"),h.TickEvidence([]))[1:]
    assert a==b


@pytest.mark.parametrize("gap,run_ids,expected",[(16,["a","a"],"INTERRUPTED"),(1,["a","b"],"INTERRUPTED"),(1,["a","a"],"INSUFFICIENT_COLLECTION_SPAN")])
def test_freshness_cannot_rehabilitate_collection_failures(gap,run_ids,expected):
    slug="btc-updown-15m-0"
    rows=[row(snapshot_ts=i*gap,market_slug=slug,run_id=run_ids[i],joint_fresh=True) for i in range(2)]
    sums={("a",slug):{"canonical_settlement_side":"UP","settlement_reference_is_canonical":True}}
    assert h.gate(slug,rows,sums,{},1000)[0]==expected


def test_exact_strike_is_asof_and_same_run():
    r=row(run_id="a",market_slug="s",strike=1000)
    assert not h.exact(r,{("a","s"):[(101,1000)]})
    assert not h.exact(r,{("b","s"):[(99,1000)]})
    assert h.exact(r,{("a","s"):[(99,1000)]})


def test_checkpoint_reuses_twelve_second_nearest_rule():
    assert h.ra._checkpoint_row([{"time_left_sec":313}],300) is None
    assert h.ra._checkpoint_row([{"time_left_sec":312}],300)=={"time_left_sec":312}


def test_binomial_statistics_reproduce_old_primary_comparison():
    a=h.binary([True]*13+[False]*40);b=h.binary([True]*2+[False]*49)
    d=h.difference(a,b)
    assert d["diff_pp"]==pytest.approx(20.60673,abs=1e-5)
    assert d["fisher"]==pytest.approx(.00404,abs=1e-5)
    assert d["newcombe"][0]>0
    assert h.wilson(0,51)[1]>0


def test_offline_sqlite_read_is_immutable(tmp_path):
    p=tmp_path/"test.db"
    with sqlite3.connect(p) as c:c.execute("CREATE TABLE x (a)")
    before=h.digest(p)
    with h.readonly(p) as c:
        assert c.execute("SELECT count(*) FROM x").fetchone()[0]==0
        with pytest.raises(sqlite3.OperationalError):c.execute("INSERT INTO x VALUES (1)")
    assert before==h.digest(p) and not list(tmp_path.glob("*-wal"))


def test_run_rejects_active_database_paths(tmp_path):
    with pytest.raises(ValueError,match="offline"):
        h.run(tmp_path/"active.db",tmp_path/"journal.db",tmp_path,tmp_path)


def test_parquet_only_last_endpoint_used_and_inputs_unchanged(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    p=tmp_path/"BTCUSDT_1s_2026-10-06_part-1.parquet"
    pq.write_table(pa.Table.from_pylist([{"open":50.,"close":51.,"first_source_ts":100600,
                    "last_source_ts":100700,"first_received_ts":100300,"last_received_ts":100400}]),p)
    before=h.digest(p)
    ticks,inventory,errors=h.load_tick_evidence(tmp_path,{"2026-10-06"})
    assert not errors and len(inventory)==1 and h.digest(p)==before
    assert ticks.exact(100.6,100.5)[1]=="NOT_RECOMPUTABLE"
    assert ticks.exact(100.7,100.5)[0]["price"]==51
