from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any

import requests


WORKFLOW_PREFIX = "Experiment: "
QUERY_NODE_NAME = "Query Webhook"
EXECUTIONS_PAGE_LIMIT = 250


class DashboardError(Exception):
    pass


@dataclass(frozen=True)
class ExperimentStatus:
    name: str
    completed: int
    total: int
    progress_text: str
    progress_percent: int
    started_at: str | None
    started_at_label: str
    status: str
    status_label: str
    query_url: str | None


def list_experiments(timeout_seconds: int = 5) -> list[ExperimentStatus]:
    session = _build_session()
    base_url = _require_env("N8N_URL").rstrip("/")
    workflows = _fetch_workflows(session, base_url, timeout_seconds)

    experiments = [
        _load_experiment(session, base_url, workflow, timeout_seconds)
        for workflow in workflows
        if str(workflow.get("name", "")).startswith(WORKFLOW_PREFIX)
    ]
    return sorted(experiments, key=_sort_key)


def clear_old_runs(timeout_seconds: int = 5, older_than_days: int = 3) -> int:
    session = _build_session()
    base_url = _require_env("N8N_URL").rstrip("/")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    deleted = 0
    cursor: str | None = None
    while True:
        page, cursor = _fetch_executions_page(session, base_url, timeout_seconds, cursor)
        if not page:
            break
        for execution in page:
            started_at = _parse_datetime(execution.get("startedAt"))
            if started_at is None or started_at >= cutoff:
                continue
            if _delete_execution(session, base_url, execution.get("id"), timeout_seconds):
                deleted += 1
        if not cursor:
            break
    return deleted


def _fetch_executions_page(
    session: requests.Session,
    base_url: str,
    timeout_seconds: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"limit": EXECUTIONS_PAGE_LIMIT}
    if cursor:
        params["cursor"] = cursor
    try:
        response = session.get(
            f"{base_url}/api/v1/executions", params=params, timeout=timeout_seconds
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DashboardError(f"Could not load executions from n8n: {exc}") from exc

    payload = response.json()
    if not isinstance(payload, dict):
        raise DashboardError("Unexpected executions response from n8n.")
    data = payload.get("data")
    if not isinstance(data, list):
        raise DashboardError("Unexpected executions response from n8n.")
    next_cursor = payload.get("nextCursor")
    return data, next_cursor if isinstance(next_cursor, str) and next_cursor else None


def _delete_execution(
    session: requests.Session,
    base_url: str,
    execution_id: Any,
    timeout_seconds: int,
) -> bool:
    if execution_id is None:
        return False
    try:
        response = session.delete(
            f"{base_url}/api/v1/executions/{execution_id}", timeout=timeout_seconds
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DashboardError(f"Failed to delete execution {execution_id}: {exc}") from exc
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "X-N8N-API-KEY": _require_env("N8N_API_KEY"),
            "Content-Type": "application/json",
            "User-Agent": "experiment-dashboard/1.0",
        }
    )
    return session


def _fetch_workflows(session: requests.Session, base_url: str, timeout_seconds: int) -> list[dict[str, Any]]:
    try:
        response = session.get(f"{base_url}/api/v1/workflows", timeout=timeout_seconds)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DashboardError(f"Could not load workflows from n8n: {exc}") from exc

    payload = response.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    raise DashboardError("Unexpected workflows response from n8n.")


def _load_experiment(
    session: requests.Session,
    base_url: str,
    workflow: dict[str, Any],
    timeout_seconds: int,
) -> ExperimentStatus:
    query_url = _extract_query_url(base_url, workflow)
    name = str(workflow.get("name", "Unnamed workflow"))[len(WORKFLOW_PREFIX):] or "Unnamed experiment"

    if query_url is None:
        return _unavailable_experiment(name, None)

    try:
        response = session.get(query_url, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return _unavailable_experiment(name, query_url)

    completed = _to_int(payload.get("completed"))
    total = _to_int(payload.get("total"))
    started_at = payload.get("started_at") if isinstance(payload.get("started_at"), str) else None
    experiment_name = payload.get("experiment") if isinstance(payload.get("experiment"), str) else name

    status = _derive_status(completed, total, started_at)
    progress_text = payload.get("progress") if isinstance(payload.get("progress"), str) else f"{completed} / {total}"

    return ExperimentStatus(
        name=experiment_name,
        completed=completed,
        total=total,
        progress_text=progress_text,
        progress_percent=_progress_percent(completed, total),
        started_at=started_at,
        started_at_label=_format_started_at(started_at),
        status=status,
        status_label=status.replace("_", " ").title(),
        query_url=query_url,
    )


def _extract_query_url(base_url: str, workflow: dict[str, Any]) -> str | None:
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return None

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("name") != QUERY_NODE_NAME:
            continue
        parameters = node.get("parameters")
        if not isinstance(parameters, dict):
            return None
        path = parameters.get("path")
        if isinstance(path, str) and path:
            return f"{base_url}/webhook/{path}"
    return None


def _unavailable_experiment(name: str, query_url: str | None) -> ExperimentStatus:
    return ExperimentStatus(
        name=name,
        completed=0,
        total=0,
        progress_text="Unavailable",
        progress_percent=0,
        started_at=None,
        started_at_label="Unavailable",
        status="unavailable",
        status_label="Unavailable",
        query_url=query_url,
    )


def _derive_status(completed: int, total: int, started_at: str | None) -> str:
    if total == 0:
        return "empty"
    if completed >= total:
        return "completed"
    if started_at or completed > 0:
        return "running"
    return "pending"


def _progress_percent(completed: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round((completed / total) * 100)))


def _format_started_at(value: str | None) -> str:
    if not value:
        return "—"

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value

    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise DashboardError(f"Missing required environment variable: {name}")


def _sort_key(experiment: ExperimentStatus) -> float:
    return -_sort_timestamp(experiment.started_at)


def _sort_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
