"""Judge-transport containment + baseline-promoter completeness (2026-08-11).

Two seatbelts on the QA harness, David-approved:

* A dead `claude -p` subprocess (timeout, nonzero exit, garbage stdout) is an
  unusable judge SAMPLE — contained, counted, formula verdict stands — while
  systemic transport failure crashes the run loudly instead of finishing it
  silently formula-only.
* `accept_baseline` refuses partial manifests: a runner that died mid-run
  leaves a file whose name is indistinguishable from a complete run's, and a
  partial promoted to the bar blinds every future comparison to the missing
  cases.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from experiments import adjudicate, qa
from x1_advisor.agent import judge_cc
from x1_advisor.agent.judge_cc import JudgeTransportDown


@pytest.fixture(autouse=True)
def _fresh_transport_counters():
    judge_cc._TRANSPORT_STATS.update(calls=0, failures=0)
    yield
    judge_cc._TRANSPORT_STATS.update(calls=0, failures=0)


# --- containment: one dead call is a discarded sample, not a dead run ------


def _with_cli(monkeypatch, fake_run):
    monkeypatch.setattr(judge_cc.shutil, "which", lambda *a, **k: "/bin/claude")
    monkeypatch.setattr(judge_cc.subprocess, "run", fake_run)


def test_timeout_is_contained_and_counted(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    _with_cli(monkeypatch, fake_run)
    assert judge_cc._run_claude("p", tracker=None, stage="t") is None
    assert judge_cc.transport_stats() == {"calls": 1, "failures": 1}


def test_nonzero_exit_is_contained(monkeypatch):
    _with_cli(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="api error"))
    assert judge_cc._run_claude("p", tracker=None, stage="t") is None
    assert judge_cc.transport_stats()["failures"] == 1


def test_garbage_stdout_is_contained(monkeypatch):
    _with_cli(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="<html>proxy error</html>", stderr=""))
    assert judge_cc._run_claude("p", tracker=None, stage="t") is None


def test_missing_cli_still_fails_fast(monkeypatch):
    # a config error is not a flake — never contained
    monkeypatch.setattr(judge_cc.shutil, "which", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        judge_cc._run_claude("p", tracker=None, stage="t")
    assert judge_cc.transport_stats() == {"calls": 0, "failures": 0}


def test_healthy_calls_keep_failure_rate_below_tripwire(monkeypatch):
    _with_cli(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"result": "{}"}), stderr=""))
    for _ in range(50):
        assert judge_cc._run_claude("p", tracker=None, stage="t") is not None
    assert judge_cc.transport_stats() == {"calls": 50, "failures": 0}


# --- tripwire: systemic failure crashes loudly -----------------------------


def test_systemic_failure_trips_judge_down(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    _with_cli(monkeypatch, fake_run)
    # below both thresholds nothing raises; the breach raises
    with pytest.raises(JudgeTransportDown, match="judge is down"):
        for _ in range(judge_cc.CC_MAX_TRANSPORT_FAILURES + 1):
            judge_cc._run_claude("p", tracker=None, stage="t")


def test_isolated_failures_in_a_long_run_never_trip(monkeypatch):
    # 3 dead calls scattered across 100 = 3%: contained, no crash. (Three
    # dead in a ROW at run start DOES trip — 3-for-3 is an outage signature,
    # and crashing on call 3 is cheaper than crashing on call 90.)
    healthy = SimpleNamespace(returncode=0,
                              stdout=json.dumps({"result": "{}"}), stderr="")
    fail_at = {30, 60, 90}
    outcomes = iter(i not in fail_at for i in range(100))

    def fake_run(*a, **k):
        if not next(outcomes):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        return healthy

    _with_cli(monkeypatch, fake_run)
    results = [judge_cc._run_claude("p", tracker=None, stage="t")
               for _ in range(100)]
    assert sum(1 for r in results if r is None) == 3
    assert judge_cc.transport_stats() == {"calls": 100, "failures": 3}


def test_all_samples_dead_for_one_item_is_an_outage(monkeypatch):
    dead = lambda prompt, *, tracker=None, stage="": None
    with pytest.raises(JudgeTransportDown, match="outage"):
        adjudicate._samples("p", adjudicate._CitationVerdicts, tracker=None,
                            stage="t", transport=dead)


def test_partial_sample_death_keeps_surviving_votes(monkeypatch):
    ok = {"result": json.dumps({"verdicts": [
        {"id": 1, "adequate": True, "reason": "fine"}]})}
    outs = iter([None, ok, ok])
    flaky = lambda prompt, *, tracker=None, stage="": next(outs)
    parsed, _ = adjudicate._samples("p", adjudicate._CitationVerdicts,
                                    tracker=None, stage="t", transport=flaky)
    assert len(parsed) == 2                 # dead vote discarded, not fatal


# --- promoter: a pointer must be proof of a complete run -------------------


_CONTRACT = "golden-v2.0/s6/modes-test"


def _stub_suite():
    return SimpleNamespace(
        cases=[SimpleNamespace(tier="smoke")] * 2
              + [SimpleNamespace(tier="core")] * 3,
        scripts=[SimpleNamespace()],
        version="v2.0", contract=_CONTRACT, digest="d" * 64)


def _write_manifest(runs, name, n_rows, *, summary=True, contract=_CONTRACT):
    lines = [json.dumps({"case_id": f"c{i}", "scoring_contract": contract})
             for i in range(n_rows)]
    if summary:
        lines.append(json.dumps({"record": "summary", "identity_drift": False,
                                 "scoring_contract": contract}))
    (runs / name).write_text("\n".join(lines) + "\n")


@pytest.fixture()
def promoter(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(qa, "RUNS_DIR", runs)
    monkeypatch.setattr(qa, "BASELINE_POINTER", tmp_path / "baseline.json")
    monkeypatch.setattr(qa, "load_suite", lambda v: _stub_suite())
    monkeypatch.setattr(qa, "read_manifest", lambda: {"truth_sets": {}})
    return runs


def test_complete_run_is_accepted(promoter, tmp_path):
    _write_manifest(promoter, "2026-08-11_v2_core_abc_r1.jsonl", 3)
    qa.accept_baseline(["2026-08-11_v2_core_abc_r1.jsonl"])
    pointer = json.loads((tmp_path / "baseline.json").read_text())
    assert pointer["manifests"] == {"core": "2026-08-11_v2_core_abc_r1.jsonl"}
    assert pointer["scoring_contract"] == _CONTRACT


def test_partial_run_is_refused_with_the_count(promoter):
    _write_manifest(promoter, "2026-08-11_v2_core_abc_r1.jsonl", 2)
    with pytest.raises(SystemExit, match="2/3 core rows"):
        qa.accept_baseline(["2026-08-11_v2_core_abc_r1.jsonl"])


def test_missing_summary_row_is_refused(promoter):
    # the runner died before writing the summary — the classic partial file
    _write_manifest(promoter, "2026-08-11_v2_smoke_abc_r1.jsonl", 2,
                    summary=False)
    with pytest.raises(SystemExit, match="no summary row"):
        qa.accept_baseline(["2026-08-11_v2_smoke_abc_r1.jsonl"])


def test_stale_contract_is_refused(promoter):
    _write_manifest(promoter, "2026-08-11_scripts_v2.0_abc_r1.jsonl", 1,
                    contract="golden-v2.0/s5/modes-old")
    with pytest.raises(SystemExit, match="current suite contract"):
        qa.accept_baseline(["2026-08-11_scripts_v2.0_abc_r1.jsonl"])
