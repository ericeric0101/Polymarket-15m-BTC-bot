"""Exercise the actual nested timer without starting a node or real threads."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import launcher


def _timer_source():
    module = ast.parse(Path(launcher.__file__).read_text())
    run = next(n for n in module.body if isinstance(n, ast.FunctionDef)
               and n.name == "_run_integrated_bot_cycles")
    assignments = [n for n in ast.walk(run) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "auto_rollover_sec"
                           for t in n.targets)]
    assert len(assignments) == 1
    worker = next(n for n in ast.walk(run) if isinstance(n, ast.FunctionDef)
                  and n.name == "_rollover_worker")
    return assignments[0], worker


def _run_timer(protected=False, market_phase_defer=None):
    assignment, worker = _timer_source()
    stopped = []
    clock = SimpleNamespace(now=0)
    strategy = SimpleNamespace(
        inventory_delta_shares=5 if protected else 0,
        maker_exchange_min_shares=5,
        active_maker_orders={},
        _request_node_stop_callback=lambda: stopped.append(clock.now),
    )
    node = SimpleNamespace(trader=SimpleNamespace(strategies=lambda: [strategy]))
    waits = []

    class VirtualEvent:
        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                assert timeout == 10800
                # Advance through the old deadline and just before the new
                # deadline while the event is still waiting, with no stop.
                for elapsed in (3600, 10799):
                    clock.now = elapsed
                    assert clock.now < timeout
                    assert stopped == []
                clock.now = timeout
                return False
            assert protected and waits == [10800, 5.0]
            assert stopped == []
            strategy.inventory_delta_shares = 0
            clock.now += timeout
            return False

    requested = []
    namespace = {
        "node": node,
        "rollover_stop": VirtualEvent(),
        "rollover_requested": SimpleNamespace(set=lambda: requested.append(clock.now)),
        "time": SimpleNamespace(time=lambda: clock.now),
        "logger": SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
        "_strategy_rollover_exposure_reasons": launcher._strategy_rollover_exposure_reasons,
        "request_auto_rollover_stop": launcher.request_auto_rollover_stop,
        "_rollover_market_phase_defer_sec": market_phase_defer or (lambda now: 0.0),
    }
    # Compile the production assignment and worker unchanged into a closure;
    # its nonlocal rollover_source has the same binding as in the launcher.
    wrapper = ast.parse(
        "def make_worker():\n    rollover_source = None\n"
        "    return None\n"
    ).body[0]
    wrapper.body[1:2] = [assignment, worker,
                          ast.Return(value=ast.Name(id="_rollover_worker", ctx=ast.Load()))]
    tree = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(tree, launcher.__file__, "exec"), namespace)
    namespace["make_worker"]()()
    assert requested == stopped
    assert strategy._collection_stop_context == {"stop_request_source": "scheduled_auto_rollover"}
    return waits, stopped


def test_single_interval_authority_ignores_retired_environment_override(monkeypatch):
    monkeypatch.setenv("AUTO_NODE_ROLLOVER_SEC", "3600")
    assignment, worker = _timer_source()
    assert isinstance(assignment.value, ast.Constant)
    assert assignment.value.value == 10800
    initial_wait = next(n for n in worker.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "wait_sec"
                                for t in n.targets))
    assert isinstance(initial_wait.value, ast.Name)
    assert initial_wait.value.id == "auto_rollover_sec"
    assert _run_timer() == ([10800], [10800])


@pytest.mark.parametrize("protected", [False, True])
def test_scheduled_timer_and_existing_exposure_defer(protected):
    waits, stopped = _run_timer(protected)
    assert waits == ([10800, 5.0] if protected else [10800])
    assert stopped == ([10805] if protected else [10800])


def test_manual_stop_is_immediate_and_independent_of_scheduled_timer():
    stopped = []
    node = SimpleNamespace(
        trader=SimpleNamespace(strategies=lambda: []),
        kernel=SimpleNamespace(loop=None),
        stop=lambda: stopped.append("manual"),
    )
    callback = launcher.threadsafe_node_stop_callback(node)
    assert callback() is True
    assert callback() is False
    assert stopped == ["manual"]


@pytest.mark.parametrize("now_ts,expected", [
    (1791992700 + 0, 30.0),      # market open: wait for strike capture
    (1791992700 + 29, 1.0),
    (1791992700 + 30, 0.0),
    (1791992700 + 780, 0.0),
    (1791992700 + 781, 149.0),   # final 119s: wait until next open + 30s
    (1791992700 + 895, 35.0),
])
def test_market_phase_defer_keeps_close_and_open_windows_clear(now_ts, expected):
    assert launcher._rollover_market_phase_defer_sec(now_ts) == pytest.approx(expected)


def test_scheduled_timer_defers_out_of_the_late_market_window():
    assignment, worker = _timer_source()
    calls = []

    def defer(now):
        calls.append(now)
        return 149.0 if len(calls) == 1 else 0.0

    # Reuse the virtual timer: the first market-phase check defers once.
    stopped = []
    clock = SimpleNamespace(now=0)
    strategy = SimpleNamespace(
        inventory_delta_shares=0, maker_exchange_min_shares=5, active_maker_orders={},
        _request_node_stop_callback=lambda: stopped.append(clock.now),
    )
    node = SimpleNamespace(trader=SimpleNamespace(strategies=lambda: [strategy]))
    waits = []

    class VirtualEvent:
        def wait(self, timeout):
            waits.append(timeout)
            clock.now += timeout
            return False

    namespace = {
        "node": node, "rollover_stop": VirtualEvent(),
        "rollover_requested": SimpleNamespace(set=lambda: None),
        "time": SimpleNamespace(time=lambda: clock.now),
        "logger": SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
        "_strategy_rollover_exposure_reasons": launcher._strategy_rollover_exposure_reasons,
        "request_auto_rollover_stop": launcher.request_auto_rollover_stop,
        "_rollover_market_phase_defer_sec": defer,
    }
    wrapper = ast.parse("def make_worker():\n    rollover_source = None\n    return None\n").body[0]
    wrapper.body[1:2] = [assignment, worker, ast.Return(value=ast.Name(id="_rollover_worker", ctx=ast.Load()))]
    tree = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(tree, launcher.__file__, "exec"), namespace)
    namespace["make_worker"]()()
    assert waits == [10800, 149.0]
    assert stopped == [10949.0]
