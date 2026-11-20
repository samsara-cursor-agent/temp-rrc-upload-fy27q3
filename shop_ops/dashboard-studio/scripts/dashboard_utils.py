"""Shared utilities for dashboard-studio pull/push scripts."""

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent.parent  # dataplatform/dashboards/

SYNC_STATE_FILE = ".dashboard_sync_state.json"

# Build-attribution signature. Injected programmatically into every dataset's
# SQL (by add_dataset and db_push) so dashboards built with the
# datatools-dashboard-build skill can be identified later. Stable and
# version-less so it never churns query diffs, and durable because dataset
# queryLines survive the Databricks export/import roundtrip verbatim.
SIGNATURE_TOKEN = "@samsara:built-with=datatools-dashboard-build"
SIGNATURE_LINE = f"-- {SIGNATURE_TOKEN}"


def ensure_sql_signature(sql: str) -> str:
    """Idempotently prepend the build-signature comment to a SQL string.

    Returns the SQL unchanged if the signature token appears anywhere in it,
    otherwise prepends the signature line. Matching anywhere (rather than only
    at the head) keeps this helper a true no-op once a dataset is signed, so
    repeated pushes never duplicate the token, and it mirrors the same
    ``SIGNATURE_TOKEN in sql`` check used by db_push and validate_lvdash. The
    practical effect is that a token already present in the SQL body suppresses
    stamping -- acceptable here since the token is purpose-built for this
    tooling and is not expected to occur naturally in dataset queries.
    """
    if SIGNATURE_TOKEN in sql:
        return sql
    body = sql.lstrip("\n")
    return f"{SIGNATURE_LINE}\n{body}"


def stable_hash(obj: dict) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_sync_state(
    dashboard_dir: Path,
    databricks_path: str,
    env: str,
    remote_hash: str,
    *,
    timestamp_key: str = "last_pull_at_utc",
    region: str = None,
):
    state = {
        "databricks_path": databricks_path,
        "env": env,
        "region": region or "us",
        timestamp_key: datetime.now(timezone.utc).isoformat(),
        "last_pulled_remote_hash": remote_hash,
    }
    state_path = dashboard_dir / SYNC_STATE_FILE
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def find_dashboard_dir(dashboard_name: str) -> Path:
    """Find the dashboard directory under dashboard-studio-dashboards/."""
    candidate = DASHBOARDS_DIR / "dashboard-studio-dashboards" / dashboard_name

    if candidate.exists():
        return candidate

    print(
        f"Error: Dashboard '{dashboard_name}' not found.\n"
        f"Expected at: {candidate}\n"
        f"Create it first using the dashboard-studio quickstart flow.",
        file=sys.stderr,
    )
    sys.exit(1)


def get_databricks_path(dashboard_dir: Path, env: str, region: str = None) -> str:
    """Read the databricks_path from dashboard_config.json for the given environment and region.

    Looks for keys in this order:
        1. databricks_path_{env}_{region}  (e.g. databricks_path_dev_eu)
        2. databricks_path_{env}           (e.g. databricks_path_dev) -- only for \"us\" or no region
    """
    config_path = dashboard_dir / "dashboard_config.json"
    if not config_path.exists():
        print(
            f"Error: No dashboard_config.json found at {config_path}.\n"
            f"This file must contain 'databricks_path_dev' and/or 'databricks_path_prod'.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    if region and region != "us":
        path_key = f"databricks_path_{env}_{region}"
        db_path = config.get(path_key)
        if db_path:
            return db_path
        available = [k for k in config if k.startswith("databricks_path_")]
        print(
            f"Error: '{path_key}' is not configured in {config_path}.\n"
            f"Available paths: {available if available else 'none'}\n"
            f"Add it to dashboard_config.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    path_key = f"databricks_path_{env}"
    db_path = config.get(path_key)
    if not db_path:
        available = [k for k in config if k.startswith("databricks_path_")]
        print(
            f"Error: '{path_key}' is not configured in {config_path}.\n"
            f"Available paths: {available if available else 'none'}\n"
            f"Add it to dashboard_config.json.",
            file=sys.stderr,
        )
        sys.exit(1)
    return db_path


def get_databricks_warehouse(dashboard_dir: Path, env: str, region: str = None) -> str:
    """Read the configured SQL warehouse name for an environment and region."""
    config_path = dashboard_dir / "dashboard_config.json"
    if not config_path.exists():
        print(
            f"Error: No dashboard_config.json found at {config_path}.", file=sys.stderr
        )
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    region = region or "us"
    key = (
        f"databricks_warehouse_{env}_{region}"
        if region != "us"
        else f"databricks_warehouse_{env}"
    )
    warehouse = config.get(key)
    if warehouse:
        return warehouse

    print(
        f"Error: '{key}' is not configured in {config_path}.\n"
        "Add the SQL warehouse name to dashboard_config.json.",
        file=sys.stderr,
    )
    sys.exit(1)


def sanitize_sql_path(display_name: str) -> str:
    """Convert a dataset displayName to a safe filesystem path.

    Dot-separated names become directory paths:
        \"queries.overview.total_trips\" -> \"queries/overview/total_trips.sql\"

    Human-readable names are lowercased and have special chars replaced:
        \"CAN Library Traces Count\" -> \"can_library_traces_count.sql\"
        \"Email/SMS Alerts per 1M miles\" -> \"email_sms_alerts_per_1m_miles.sql\"
    """
    parts = display_name.split(".")
    sanitized = []
    for part in parts:
        s = part.lower()
        s = re.sub(r"[^a-z0-9_]", "_", s)
        s = re.sub(r"_+", "_", s)
        s = s.strip("_")
        if s:
            sanitized.append(s)
    return "/".join(sanitized) + ".sql" if sanitized else ""
