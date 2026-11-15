# Tests de l'enregistrement des échecs Airflow (src/monitor/failure_callback.py).
# Aucune base, aucun Airflow : contextes simulés, fichier dans tmp_path (FAILURES_LOG).
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.monitor import failure_callback
from src.monitor.failure_callback import record_event, record_failure

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def failures(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "failures.jsonl"
    monkeypatch.setenv("FAILURES_LOG", str(path))
    return path


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_task_failure_is_recorded_without_exception_text(failures):
    ti = SimpleNamespace(task_id="analyze_news", dag_id="card_market_news", try_number=2)
    ctx = {
        "task_instance": ti,
        "dag": SimpleNamespace(dag_id="card_market_news"),
        "run_id": "scheduled__2026-11-12T12:00:00+00:00",
        "exception": RuntimeError("http://x/?api_key=SECRET"),
    }
    record_failure(ctx)
    (rec,) = _lines(failures)
    assert rec["kind"] == "task_failed"
    assert (rec["dag_id"], rec["task_id"], rec["try_number"]) == ("card_market_news", "analyze_news", 2)
    assert rec["run_id"].startswith("scheduled__")
    assert rec["detail"] == "RuntimeError"
    assert "SECRET" not in failures.read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["ts"])


def test_dag_failure_has_no_task(failures):
    dag_run = SimpleNamespace(dag_id="card_catalog_refresh", run_id="scheduled__2026-11-09")
    record_failure({"dag_run": dag_run, "reason": "timed_out"})
    (rec,) = _lines(failures)
    assert rec["kind"] == "dag_failed" and rec["task_id"] is None
    assert (rec["dag_id"], rec["run_id"], rec["detail"]) == (
        "card_catalog_refresh",
        "scheduled__2026-11-09",
        "timed_out",
    )


def test_records_are_appended(failures):
    record_failure({"dag_run": SimpleNamespace(dag_id="a", run_id="1"), "reason": "failed"})
    record_failure({"dag_run": SimpleNamespace(dag_id="b", run_id="2"), "reason": "failed"})
    assert [r["dag_id"] for r in _lines(failures)] == ["a", "b"]


def test_record_event_for_stale_prices(failures):
    record_event("prices_stale", "card_price_pipeline", "prix figés", "check_freshness")
    (rec,) = _lines(failures)
    assert rec["kind"] == "prices_stale" and rec["task_id"] == "check_freshness"
    assert rec["detail"] == "prix figés"


def test_callback_never_raises(failures, monkeypatch):
    record_failure(None)  # contexte inattendu
    record_failure({"task_instance": object(), "exception": ValueError("x")})
    # fichier non inscriptible : on journalise, on ne lève pas
    blocker = failures.parent.parent / "bloque"
    blocker.write_text("un fichier, pas un dossier")
    monkeypatch.setenv("FAILURES_LOG", str(blocker / "f.jsonl"))
    record_failure({"dag_run": SimpleNamespace(dag_id="a", run_id="1")})
    record_event("k", "d", "m")


def test_default_path_is_under_airflow_logs(monkeypatch):
    monkeypatch.delenv("FAILURES_LOG", raising=False)
    monkeypatch.setenv("AIRFLOW_HOME", "/opt/airflow")
    assert failure_callback.failures_path() == Path("/opt/airflow/logs/failures.jsonl")


def test_dags_wire_the_callback_as_designed():
    # Les DAG ne s'importent pas hors Airflow : on vérifie le câblage dans le source.
    def src(name):
        return (ROOT / "dags" / name).read_text(encoding="utf-8")

    for name in ("card_price_pipeline_dag.py", "card_market_news_dag.py"):
        text = src(name)
        assert "on_failure_callback=record_failure" in text  # DagRun
        assert 'default_args={"on_failure_callback": record_failure}' in text  # tâches
    catalog = src("card_catalog_refresh_dag.py")
    assert "on_failure_callback=record_failure" in catalog  # échec final du DagRun seulement
    assert "default_args={" not in catalog  # pas de callback sur les tâches (retries=60)
    assert "send_ntfy" not in src("card_price_pipeline_dag.py")
