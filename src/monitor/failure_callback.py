# Enregistrement des échecs Airflow, SANS ntfy (règle du 2026-11-15 : un seul
# message quotidien, envoyé par ~/bin/check-jobs.sh qui lit ce que les tâches
# enregistrent). Deux usages :
#   - record_failure : à brancher en on_failure_callback, sur le DAG (échec du
#     DagRun) et sur les tâches (default_args). Airflow n'appelle le callback
#     d'une tâche qu'à son échec FINAL, pas à chaque retry : pour
#     card_catalog_refresh (retries=60) on ne branche que le callback du DAG.
#   - record_event : pour un signal qui ne fait pas échouer le DagRun
#     (check_freshness : prix figés par la source).
# Chaque appel écrit une ligne JSON dans failures.jsonl (dossier logs partagé
# avec l'hôte : ~/card-price-tracker/logs/failures.jsonl) et dans le log Airflow.
# Jamais d'exception ici : un callback qui plante masquerait l'échec d'origine.
# Aucun secret : seul le NOM du type d'exception est conservé, pas son texte.
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def failures_path() -> Path:
    explicit = os.environ.get("FAILURES_LOG")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("AIRFLOW_HOME", "/opt/airflow")) / "logs" / "failures.jsonl"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    log.error("ECHEC_ENREGISTRE %s", line)
    try:
        path = failures_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, ValueError):
        log.exception("failures.jsonl non écrit")


def record_event(kind: str, dag_id: str, detail: str, task_id: str | None = None) -> None:
    try:
        _append(
            {
                "ts": _now(),
                "kind": kind,
                "dag_id": dag_id,
                "task_id": task_id,
                "run_id": None,
                "detail": detail,
            }
        )
    except Exception:  # noqa: BLE001 - un callback ne doit jamais lever
        log.exception("record_event a échoué")


def record_failure(context: dict[str, Any]) -> None:
    """on_failure_callback commun : niveau « task » si le contexte porte une tâche, sinon « dag »."""
    try:
        ti = context.get("task_instance")
        dag_run = context.get("dag_run")
        dag = context.get("dag")
        dag_id = getattr(dag, "dag_id", None) or getattr(dag_run, "dag_id", None)
        run_id = context.get("run_id") or getattr(dag_run, "run_id", None)
        exc = context.get("exception")
        record: dict[str, Any] = {
            "ts": _now(),
            "kind": "task_failed" if ti is not None else "dag_failed",
            "dag_id": dag_id or getattr(ti, "dag_id", None),
            "task_id": getattr(ti, "task_id", None),
            "run_id": run_id,
            "try_number": getattr(ti, "try_number", None),
            "detail": type(exc).__name__ if exc is not None else context.get("reason"),
        }
        _append(record)
    except Exception:  # noqa: BLE001 - un callback ne doit jamais lever
        log.exception("record_failure a échoué")
