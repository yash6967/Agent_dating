"""Persistent, background cohort workflows for the operator-run POC."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from core import (
    add_person,
    analyze_person,
    connect,
    latest_date,
    latest_profile,
    list_people,
    now,
    person,
    plan_autonomous_pairs,
    rank_for,
    refresh_source,
    run_date,
    sources,
    uid,
)


_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def enqueue(kind: str, config: dict | None = None) -> str:
    if kind not in {"analyze_cohort", "date_cohort"}:
        raise ValueError("Unknown cohort workflow.")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Configure OPENAI_API_KEY for model-powered profile agents and autonomous dates.")
    if kind == "analyze_cohort" and not os.getenv("APIFY_TOKEN"):
        raise RuntimeError("Configure APIFY_TOKEN to read LinkedIn and Instagram profiles.")
    db = connect()
    try:
        active = db.execute(
            "SELECT id FROM pipeline_runs WHERE kind=? AND status IN ('queued', 'running') ORDER BY created_at LIMIT 1",
            (kind,),
        ).fetchone()
        if active:
            return active["id"]
        run_id = uid()
        stamp = now()
        with db:
            db.execute(
                """INSERT INTO pipeline_runs(id, kind, status, phase, config_json, created_at, updated_at)
                   VALUES(?, ?, 'queued', 'queued', ?, ?, ?)""",
                (run_id, kind, json.dumps(config or {}), stamp, stamp),
            )
    finally:
        db.close()
    ensure_worker()
    return run_id


def ensure_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker_loop, name="between-agents-worker", daemon=True)
        _WORKER.start()


def _update(db: sqlite3.Connection, run_id: str, *, phase: str | None = None, done: int | None = None,
            total: int | None = None, status: str | None = None, errors: list[str] | None = None) -> None:
    row = db.execute("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return
    new_status = status or row["status"]
    with db:
        db.execute(
            """UPDATE pipeline_runs SET status=?, phase=?, progress_done=?, progress_total=?, error_json=?,
               updated_at=?, completed_at=CASE WHEN ? IN ('complete','complete_with_errors','failed') THEN ? ELSE completed_at END
               WHERE id=?""",
            (new_status, phase or row["phase"], done if done is not None else row["progress_done"],
             total if total is not None else row["progress_total"],
             json.dumps(errors if errors is not None else json.loads(row["error_json"])), now(),
             new_status, now(), run_id),
        )


def _worker_loop() -> None:
    db = connect()
    try:
        # Resume unfinished work after an app process restart. Ingestion is
        # idempotent; date pairs with completed IDs are skipped below.
        with db:
            db.execute("UPDATE pipeline_runs SET status='queued', phase='resuming' WHERE status='running'")
            db.execute("UPDATE autonomous_date_plan SET status='queued' WHERE status='running'")
        while True:
            row = db.execute(
                "SELECT * FROM pipeline_runs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                time.sleep(1)
                continue
            try:
                _update(db, row["id"], status="running", phase="starting")
                if row["kind"] == "analyze_cohort":
                    _analyze_cohort(db, row)
                else:
                    _date_cohort(db, row)
            except Exception as exc:
                errors = json.loads(db.execute("SELECT error_json FROM pipeline_runs WHERE id=?", (row["id"],)).fetchone()[0])
                errors.append(str(exc)[:500])
                _update(db, row["id"], status="failed", phase="failed", errors=errors)
    finally:
        db.close()


def _analyze_cohort(db: sqlite3.Connection, run: sqlite3.Row) -> None:
    config = json.loads(run["config_json"])
    selected = set(config.get("person_ids") or [])
    people = [p for p in list_people(db) if not selected or p["id"] in selected]
    if not people:
        raise ValueError("Add LinkedIn and Instagram pairs before running analysis.")
    force = bool(config.get("force", False))
    token = os.environ["APIFY_TOKEN"]
    total = len(people) * 3
    done = 0
    errors: list[str] = []
    _update(db, run["id"], phase="reading sources", total=total, done=0)
    for p in people:
        pair = sources(db, p["id"])
        for platform in ("linkedin", "instagram"):
            source = pair[platform]
            try:
                due = source["next_due_at"] is None or source["next_due_at"] <= now()
                if force or source["status"] != "ready" or due:
                    refresh_source(db, source["id"], token)
            except Exception as exc:
                errors.append(f"{p['display_name']} {platform}: {exc}"[:500])
            done += 1
            _update(db, run["id"], done=done, errors=errors)
        _update(db, run["id"], phase="creating agents")
        try:
            profile_id = analyze_person(db, p["id"])
            if not profile_id:
                raise RuntimeError("No profile version was created.")
        except Exception as exc:
            if "insufficient_quota" in str(exc) or "credit_balance_exhausted" in str(exc):
                raise RuntimeError("OpenAI API credits are exhausted. Add credits to the API account, then start cohort analysis again.") from exc
            errors.append(f"{p['display_name']} analysis: {exc}"[:500])
        done += 1
        _update(db, run["id"], done=done, errors=errors, phase="reading sources")
    _update(db, run["id"], status="complete_with_errors" if errors else "complete",
            phase="finished with source or analysis errors" if errors else "profiles ready", done=done, errors=errors)


def _date_cohort(db: sqlite3.Connection, run: sqlite3.Row) -> None:
    config = json.loads(run["config_json"])
    unready = [p["display_name"] for p in list_people(db) if p["status"] != "ready" or not latest_profile(db, p["id"])]
    if unready:
        raise ValueError(f"Analyze every cohort member first. Not ready: {', '.join(unready[:8])}")
    ready = [p for p in list_people(db) if p["status"] == "ready"]
    if len(ready) < 2:
        raise ValueError("At least two agents are needed for a date.")
    existing = db.execute(
        "SELECT COUNT(*) FROM autonomous_date_plan WHERE run_id=?", (run["id"],)
    ).fetchone()[0]
    if not existing:
        pairs = plan_autonomous_pairs(db, int(config.get("dates_per_agent", 1)))
        with db:
            db.executemany(
                """INSERT INTO autonomous_date_plan(id, run_id, person_a_id, person_b_id, priority, pre_score)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                [(uid(), run["id"], a, b, priority, score) for priority, (a, b, score) in enumerate(pairs, 1)],
            )
    plan = db.execute(
        "SELECT * FROM autonomous_date_plan WHERE run_id=? ORDER BY priority", (run["id"],)
    ).fetchall()
    total = len(plan) + len(ready)
    done = sum(row["status"] == "complete" for row in plan)
    errors = json.loads(run["error_json"])
    _update(db, run["id"], phase="agents dating", total=total, done=done)
    for item in plan:
        if item["status"] == "complete":
            continue
        with db:
            db.execute("UPDATE autonomous_date_plan SET status='running', error=NULL WHERE id=?", (item["id"],))
        try:
            saved = latest_date(db, item["person_a_id"], item["person_b_id"])
            date_id = saved["id"] if saved else run_date(db, item["person_a_id"], item["person_b_id"])
            with db:
                db.execute(
                    "UPDATE autonomous_date_plan SET status='complete', date_id=? WHERE id=?",
                    (date_id, item["id"]),
                )
        except Exception as exc:
            errors.append(f"Date {item['priority']}: {exc}"[:500])
            with db:
                db.execute("UPDATE autonomous_date_plan SET status='error', error=? WHERE id=?", (str(exc)[:500], item["id"]))
        done += 1
        _update(db, run["id"], done=done, errors=errors)
    _update(db, run["id"], phase="building rankings")
    for p in ready:
        try:
            rank_for(db, p["id"])
        except Exception as exc:
            errors.append(f"Ranking {p['display_name']}: {exc}"[:500])
        done += 1
        _update(db, run["id"], done=done, errors=errors)
    _update(db, run["id"], status="complete_with_errors" if errors else "complete",
            phase="rankings ready", done=done, errors=errors)


def recent_runs(db: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


def plan_for_run(db: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT q.*, a.display_name AS person_a_name, b.display_name AS person_b_name
           FROM autonomous_date_plan q JOIN people a ON a.id=q.person_a_id
           JOIN people b ON b.id=q.person_b_id WHERE q.run_id=? ORDER BY q.priority""", (run_id,)
    ).fetchall()
