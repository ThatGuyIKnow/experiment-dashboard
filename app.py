from __future__ import annotations

import os
from flask import Flask, render_template

from n8n_client import DashboardError, list_experiments


app = Flask(__name__)


def _config() -> dict[str, str | int]:
    return {
        "title": os.environ.get("DASHBOARD_TITLE", "Experiment Dashboard"),
        "host": os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        "port": int(os.environ.get("DASHBOARD_PORT", "8080")),
        "timeout": int(os.environ.get("N8N_TIMEOUT_SECONDS", "5")),
    }


@app.get("/")
def index():
    config = _config()
    experiments = []
    error = None

    try:
        experiments = list_experiments(timeout_seconds=config["timeout"])
    except DashboardError as exc:
        error = str(exc)

    return render_template(
        "index.html",
        title=config["title"],
        experiments=experiments,
        error=error,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    config = _config()
    app.run(host=config["host"], port=config["port"], debug=False)
