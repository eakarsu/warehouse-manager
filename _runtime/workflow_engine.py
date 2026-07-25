#!/usr/bin/env python3
"""Persistent, configuration-driven product workflows for merged applications.

The engine intentionally uses only the Python standard library.  Each merged app
owns a ``workflows.json`` file while records and their immutable audit history are
stored in that app's existing SQLite database.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote


ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+ -]{0,99}$")
SUPPORTED_FIELD_TYPES = {
    "text", "string", "textarea", "number", "integer", "boolean", "date",
    "datetime", "email", "url", "json", "select", "multiselect", "multi_select",
}


class WorkflowError(Exception):
    """An expected product/API failure with an HTTP-compatible status."""

    def __init__(self, message: str, status: int = 400, code: str = "workflow_error", details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": str(self), "code": self.code}
        if self.details is not None:
            result["details"] = self.details
        return result


class ConfigError(WorkflowError):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message, 500, "invalid_workflow_config", details)


@dataclass
class Response:
    status: int
    body: Any
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _state_id(item: Any, context: str) -> str:
    value = item.get("id") if isinstance(item, dict) else item
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ConfigError(f"{context} must be an identifier containing letters, numbers, '_' or '-'.")
    return value


def validate_config(raw: Any) -> dict[str, Any]:
    """Validate and normalize the public version-1 workflows contract."""
    if not isinstance(raw, dict):
        raise ConfigError("workflows.json must contain a JSON object.")
    if raw.get("version") != 1:
        raise ConfigError("workflows.json version must be 1.")
    workflows = raw.get("workflows")
    if not isinstance(workflows, list):
        raise ConfigError("workflows must be an array.")

    normalized: list[dict[str, Any]] = []
    workflow_ids: set[str] = set()
    for index, item in enumerate(workflows):
        context = f"workflows[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be an object.")
        workflow_id = _state_id(item.get("id"), f"{context}.id")
        if workflow_id in workflow_ids:
            raise ConfigError(f"Duplicate workflow id: {workflow_id}")
        workflow_ids.add(workflow_id)
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ConfigError(f"{context}.title is required.")

        raw_fields = item.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ConfigError(f"{context}.fields must be an array.")
        fields: list[dict[str, Any]] = []
        field_keys: set[str] = set()
        for field_index, source_field in enumerate(raw_fields):
            field_context = f"{context}.fields[{field_index}]"
            if not isinstance(source_field, dict):
                raise ConfigError(f"{field_context} must be an object.")
            key = source_field.get("key")
            if not isinstance(key, str) or not FIELD_RE.fullmatch(key):
                raise ConfigError(f"{field_context}.key must be a safe camelCase or underscore identifier.")
            if key in field_keys:
                raise ConfigError(f"Duplicate field key {key} in {workflow_id}.")
            field_keys.add(key)
            field_type = str(source_field.get("type", "text")).lower()
            if field_type not in SUPPORTED_FIELD_TYPES:
                raise ConfigError(f"Unsupported field type {field_type} for {workflow_id}.{key}.")
            label = source_field.get("label", key)
            if not isinstance(label, str) or not label.strip():
                raise ConfigError(f"{field_context}.label must be a non-empty string.")
            options = source_field.get("options", [])
            if not isinstance(options, list):
                raise ConfigError(f"{field_context}.options must be an array.")
            option_values = [option.get("value") if isinstance(option, dict) else option for option in options]
            if any(value is None or isinstance(value, (dict, list)) for value in option_values):
                raise ConfigError(f"{field_context}.options must contain scalar values or value/label objects.")
            field = dict(source_field)
            field.update({"key": key, "label": label.strip(), "type": field_type, "required": bool(source_field.get("required", False)), "options": options})
            fields.append(field)

        raw_states = item.get("states")
        if not isinstance(raw_states, list) or not raw_states:
            raise ConfigError(f"{context}.states must be a non-empty array.")
        state_ids = [_state_id(state, f"{context}.states") for state in raw_states]
        if len(state_ids) != len(set(state_ids)):
            raise ConfigError(f"States must be unique in workflow {workflow_id}.")
        states = [dict(state) if isinstance(state, dict) else {"id": state, "label": str(state).replace("-", " ").replace("_", " ").title()} for state in raw_states]
        for state, state_id in zip(states, state_ids):
            state["id"] = state_id
            state.setdefault("label", state_id.replace("-", " ").replace("_", " ").title())

        raw_transitions = item.get("transitions", [])
        if not isinstance(raw_transitions, list):
            raise ConfigError(f"{context}.transitions must be an array.")
        transitions: list[dict[str, Any]] = []
        transition_ids: set[str] = set()
        for transition_index, source_transition in enumerate(raw_transitions):
            transition_context = f"{context}.transitions[{transition_index}]"
            if not isinstance(source_transition, dict):
                raise ConfigError(f"{transition_context} must be an object.")
            transition_id = _state_id(source_transition.get("id"), f"{transition_context}.id")
            if transition_id in transition_ids:
                raise ConfigError(f"Duplicate transition id {transition_id} in {workflow_id}.")
            transition_ids.add(transition_id)
            source_states = source_transition.get("from")
            source_states = source_states if isinstance(source_states, list) else [source_states]
            if not source_states or any(state not in state_ids and state != "*" for state in source_states):
                raise ConfigError(f"{transition_context}.from must reference an existing state.")
            destination = source_transition.get("to")
            if destination not in state_ids:
                raise ConfigError(f"{transition_context}.to must reference an existing state.")
            transition = dict(source_transition)
            transition.update({"id": transition_id, "label": str(source_transition.get("label") or transition_id.replace("-", " ").title()), "from": source_states, "to": destination})
            transitions.append(transition)

        initial_state = item.get("initialState", state_ids[0])
        if initial_state not in state_ids:
            raise ConfigError(f"{context}.initialState must reference an existing state.")
        seeds = item.get("seeds", [])
        if not isinstance(seeds, list) or any(not isinstance(seed, dict) for seed in seeds):
            raise ConfigError(f"{context}.seeds must be an array of objects.")
        workflow = dict(item)
        workflow.update({
            "id": workflow_id, "title": title.strip(), "description": str(item.get("description", "")),
            "fields": fields, "states": states, "transitions": transitions,
            "initialState": initial_state, "seeds": seeds,
        })
        normalized.append(workflow)

    resources = raw.get("resources", [])
    if not isinstance(resources, list):
        raise ConfigError("resources must be an array.")
    normalized_resources: list[dict[str, str]] = []
    resource_names: set[str] = set()
    for index, source_resource in enumerate(resources):
        context = f"resources[{index}]"
        if not isinstance(source_resource, dict):
            raise ConfigError(f"{context} must be an object.")
        name = source_resource.get("name")
        if not isinstance(name, str) or not SQL_IDENTIFIER_RE.fullmatch(name):
            raise ConfigError(f"{context}.name must be a safe SQLite identifier.")
        if name.lower() in resource_names:
            raise ConfigError(f"Duplicate compatibility resource name: {name}")
        resource_names.add(name.lower())
        resource_type = source_resource.get("type")
        if resource_type not in {"table", "view"}:
            raise ConfigError(f"{context}.type must be 'table' or 'view'.")
        workflow_id = source_resource.get("workflowId")
        if workflow_id not in workflow_ids:
            raise ConfigError(f"{context}.workflowId must reference an existing workflow.")
        normalized_resources.append({"name": name, "type": resource_type, "workflowId": workflow_id})
    return {"version": 1, "workflows": normalized, "resources": normalized_resources}


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "workflows": [], "resources": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Unable to read workflows.json: {error}") from error
    return validate_config(raw)


class WorkflowEngine:
    """Durable record, transition, resource, and audit operations."""

    def __init__(self, app_root: str | Path, database_path: str | Path | None = None):
        self.app_root = Path(app_root)
        self.database_path = Path(database_path or self.app_root / "database.sqlite")
        self.config_path = self.app_root / "workflows.json"
        self.config = load_config(self.config_path)
        self.workflows = {workflow["id"]: workflow for workflow in self.config["workflows"]}
        self.resources = self.config["resources"]
        self.config_hash = hashlib.sha256(_canonical_json(self.config).encode()).hexdigest()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_records (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_records_active
                    ON workflow_records(workflow_id, state, updated_at) WHERE deleted_at IS NULL;
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    transition_id TEXT,
                    from_state TEXT,
                    to_state TEXT,
                    actor TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_events_record
                    ON workflow_events(workflow_id, record_id, id);
                CREATE TRIGGER IF NOT EXISTS workflow_events_immutable_update
                    BEFORE UPDATE ON workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow audit events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS workflow_events_immutable_delete
                    BEFORE DELETE ON workflow_events BEGIN
                    SELECT RAISE(ABORT, 'workflow audit events are immutable');
                END;
                CREATE TABLE IF NOT EXISTS workflow_seed_registry (
                    workflow_id TEXT NOT NULL,
                    seed_index INTEGER NOT NULL,
                    seed_hash TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, seed_index)
                );
                CREATE TABLE IF NOT EXISTS workflow_runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_resource_registry (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    resource_type TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    runtime_owned INTEGER NOT NULL CHECK(runtime_owned IN (0,1)),
                    created_at TEXT NOT NULL
                );
            """)
            self._initialize_resources(connection)
            self._seed(connection)
            connection.execute(
                "INSERT INTO workflow_runtime_meta(key,value,updated_at) VALUES('config_hash',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
                "WHERE workflow_runtime_meta.value <> excluded.value",
                (self.config_hash, _now()),
            )

    def _initialize_resources(self, connection: sqlite3.Connection) -> None:
        for resource in self.resources:
            name = resource["name"]
            existing = connection.execute("SELECT type FROM sqlite_master WHERE lower(name)=lower(?)", (name,)).fetchone()
            registry = connection.execute("SELECT * FROM workflow_resource_registry WHERE name=?", (name,)).fetchone()
            # Imported source tables are user data.  Their exact name satisfies the
            # compatibility contract, but the runtime never alters or writes them.
            if existing and not registry:
                connection.execute(
                    "INSERT INTO workflow_resource_registry(name,resource_type,workflow_id,runtime_owned,created_at) VALUES(?,?,?,?,?)",
                    (name, existing["type"], resource["workflowId"], 0, _now()),
                )
                continue
            if existing and registry and not registry["runtime_owned"]:
                continue
            if existing and existing["type"] != resource["type"]:
                raise ConfigError(f"Runtime-owned resource {name} exists as {existing['type']}, expected {resource['type']}.")
            if resource["type"] == "view":
                workflow_literal = resource["workflowId"].replace("'", "''")
                connection.execute(
                    f"CREATE VIEW IF NOT EXISTS {_quoted(name)} AS SELECT id AS workflow_record_id,id,state,data_json,version,created_at,updated_at "
                    f"FROM workflow_records WHERE workflow_id='{workflow_literal}' AND deleted_at IS NULL"
                )
            else:
                connection.execute(f"""CREATE TABLE IF NOT EXISTS {_quoted(name)} (
                    id TEXT PRIMARY KEY,
                    workflow_record_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({_quoted(name)})")}
                additions = {
                    "workflow_record_id": "TEXT", "state": "TEXT", "data_json": "TEXT",
                    "version": "INTEGER", "created_at": "TEXT", "updated_at": "TEXT",
                }
                for column, sql_type in additions.items():
                    if column not in columns:
                        connection.execute(f"ALTER TABLE {_quoted(name)} ADD COLUMN {_quoted(column)} {sql_type}")
            if not registry:
                connection.execute(
                    "INSERT INTO workflow_resource_registry(name,resource_type,workflow_id,runtime_owned,created_at) VALUES(?,?,?,?,?)",
                    (name, resource["type"], resource["workflowId"], 1, _now()),
                )

    def _workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            raise WorkflowError("Workflow not found.", 404, "workflow_not_found")
        return workflow

    def _seed(self, connection: sqlite3.Connection) -> None:
        for workflow in self.workflows.values():
            for index, seed in enumerate(workflow["seeds"]):
                if connection.execute("SELECT 1 FROM workflow_seed_registry WHERE workflow_id=? AND seed_index=?", (workflow["id"], index)).fetchone():
                    continue
                seed_hash = hashlib.sha256(_canonical_json(seed).encode()).hexdigest()
                state = seed.get("state", workflow["initialState"])
                if state not in {item["id"] for item in workflow["states"]}:
                    raise ConfigError(f"Seed {index} in {workflow['id']} has unknown state {state}.")
                supplied = seed.get("values", seed.get("data"))
                if supplied is None:
                    supplied = {key: value for key, value in seed.items() if key not in {"id", "state"}}
                values = self._validate_values(workflow, supplied, creating=True)
                record_id = str(seed.get("id") or uuid.uuid4())
                if connection.execute("SELECT 1 FROM workflow_records WHERE id=?", (record_id,)).fetchone():
                    record_id = str(uuid.uuid4())
                now = _now()
                connection.execute(
                    "INSERT INTO workflow_records(id,workflow_id,state,data_json,version,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                    (record_id, workflow["id"], state, _canonical_json(values), now, now),
                )
                self._append_event(connection, workflow["id"], record_id, "seeded", None, None, state, "system-seed", 1, {"values": values})
                self._sync_resources(connection, workflow["id"], record_id)
                connection.execute(
                    "INSERT INTO workflow_seed_registry(workflow_id,seed_index,seed_hash,record_id) VALUES(?,?,?,?)",
                    (workflow["id"], index, seed_hash, record_id),
                )

    @staticmethod
    def validate_actor(actor: Any) -> str:
        actor = "local-admin" if actor in (None, "") else str(actor).strip()
        if not ACTOR_RE.fullmatch(actor):
            raise WorkflowError("X-Actor must be 1-100 safe identity characters.", 400, "invalid_actor")
        return actor

    @staticmethod
    def _option_values(field: Mapping[str, Any]) -> list[Any]:
        return [option.get("value") if isinstance(option, dict) else option for option in field.get("options", [])]

    def _coerce(self, field: Mapping[str, Any], value: Any) -> Any:
        key, field_type = field["key"], field["type"]
        if value is None:
            return None
        try:
            if field_type in {"text", "string", "textarea", "email", "url", "date", "datetime", "select"}:
                value = str(value).strip()
            if field_type == "integer":
                if isinstance(value, bool):
                    raise ValueError
                value = int(value)
            elif field_type == "number":
                if isinstance(value, bool):
                    raise ValueError
                value = float(value)
            elif field_type == "boolean":
                if isinstance(value, bool):
                    pass
                elif str(value).lower() in {"1", "true", "yes", "on"}:
                    value = True
                elif str(value).lower() in {"0", "false", "no", "off"}:
                    value = False
                else:
                    raise ValueError
            elif field_type == "json" and isinstance(value, str):
                value = json.loads(value)
            elif field_type in {"multiselect", "multi_select"}:
                if not isinstance(value, list):
                    raise ValueError
            elif field_type == "date" and value:
                date.fromisoformat(value)
            elif field_type == "datetime" and value:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif field_type == "email" and value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise ValueError
            elif field_type == "url" and value and not re.match(r"^https?://[^\s]+$", value):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise WorkflowError(f"Invalid {field_type} value for {key}.", 422, "validation_error", {"field": key})
        options = self._option_values(field)
        if options and value not in (None, ""):
            submitted = value if isinstance(value, list) else [value]
            if any(item not in options for item in submitted):
                raise WorkflowError(f"Invalid option for {key}.", 422, "validation_error", {"field": key, "allowed": options})
        return value

    def _validate_values(self, workflow: Mapping[str, Any], supplied: Any, creating: bool, existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(supplied, dict):
            raise WorkflowError("Record values must be a JSON object.", 422, "validation_error")
        field_map = {field["key"]: field for field in workflow["fields"]}
        unknown = sorted(set(supplied) - set(field_map))
        if unknown:
            raise WorkflowError("Unknown record fields.", 422, "validation_error", {"fields": unknown})
        result = dict(existing or {})
        if creating:
            for field in workflow["fields"]:
                if field["key"] not in supplied and "default" in field:
                    result[field["key"]] = field["default"]
                elif field["key"] not in supplied and "defaultValue" in field:
                    result[field["key"]] = field["defaultValue"]
        for key, value in supplied.items():
            result[key] = self._coerce(field_map[key], value)
        missing = [
            field["key"] for field in workflow["fields"] if field["required"] and
            (field["key"] not in result or result[field["key"]] is None or result[field["key"]] == "" or result[field["key"]] == [])
        ]
        if missing:
            raise WorkflowError("Required fields are missing.", 422, "validation_error", {"fields": missing})
        return result

    @staticmethod
    def _row_payload(row: sqlite3.Row, include_deleted: bool = False) -> dict[str, Any]:
        result = {
            "id": row["id"], "workflowId": row["workflow_id"], "state": row["state"],
            "values": json.loads(row["data_json"]), "version": row["version"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }
        if include_deleted:
            result["deletedAt"] = row["deleted_at"]
        return result

    def _active_record(self, connection: sqlite3.Connection, workflow_id: str, record_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workflow_records WHERE workflow_id=? AND id=? AND deleted_at IS NULL", (workflow_id, record_id)
        ).fetchone()
        if not row:
            raise WorkflowError("Record not found.", 404, "record_not_found")
        return row

    def _append_event(self, connection: sqlite3.Connection, workflow_id: str, record_id: str, event_type: str,
                      transition_id: str | None, from_state: str | None, to_state: str | None,
                      actor: str, version: int, payload: Any) -> None:
        previous = connection.execute(
            "SELECT event_hash FROM workflow_events WHERE workflow_id=? AND record_id=? ORDER BY id DESC LIMIT 1",
            (workflow_id, record_id),
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else None
        created_at = _now()
        event_material = {
            "workflowId": workflow_id, "recordId": record_id, "eventType": event_type,
            "transitionId": transition_id, "fromState": from_state, "toState": to_state,
            "actor": actor, "version": version, "payload": payload,
            "previousHash": previous_hash, "createdAt": created_at,
        }
        event_hash = hashlib.sha256(_canonical_json(event_material).encode()).hexdigest()
        connection.execute(
            "INSERT INTO workflow_events(workflow_id,record_id,event_type,transition_id,from_state,to_state,actor,version,payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (workflow_id, record_id, event_type, transition_id, from_state, to_state, actor, version,
             _canonical_json(payload), previous_hash, event_hash, created_at),
        )

    def _sync_resources(self, connection: sqlite3.Connection, workflow_id: str, record_id: str, deleted: bool = False) -> None:
        for resource in self.resources:
            if resource["workflowId"] != workflow_id or resource["type"] != "table":
                continue
            registry = connection.execute("SELECT runtime_owned FROM workflow_resource_registry WHERE name=?", (resource["name"],)).fetchone()
            if not registry or not registry["runtime_owned"]:
                continue
            name = _quoted(resource["name"])
            connection.execute(f"DELETE FROM {name} WHERE workflow_record_id=?", (record_id,))
            if deleted:
                continue
            row = connection.execute("SELECT * FROM workflow_records WHERE id=?", (record_id,)).fetchone()
            if row:
                connection.execute(
                    f"INSERT INTO {name}(id,workflow_record_id,state,data_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (row["id"], row["id"], row["state"], row["data_json"], row["version"], row["created_at"], row["updated_at"]),
                )

    def definitions(self) -> list[dict[str, Any]]:
        counts = self.dashboard()["workflows"]
        by_id = {item["id"]: item for item in counts}
        return [{**workflow, "seeds": [], "metrics": by_id.get(workflow["id"], {})} for workflow in self.workflows.values()]

    def create(self, workflow_id: str, supplied: Any, actor: Any = None) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        values = self._validate_values(workflow, supplied, creating=True)
        actor = self.validate_actor(actor)
        record_id, now = str(uuid.uuid4()), _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO workflow_records(id,workflow_id,state,data_json,version,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (record_id, workflow_id, workflow["initialState"], _canonical_json(values), now, now),
            )
            self._append_event(connection, workflow_id, record_id, "created", None, None, workflow["initialState"], actor, 1, {"values": values})
            self._sync_resources(connection, workflow_id, record_id)
            row = self._active_record(connection, workflow_id, record_id)
        return self._row_payload(row)

    def get(self, workflow_id: str, record_id: str, include_events: bool = True) -> dict[str, Any]:
        self._workflow(workflow_id)
        with self._connect() as connection:
            row = self._active_record(connection, workflow_id, record_id)
            result = self._row_payload(row)
            if include_events:
                result["events"] = self.events(workflow_id, record_id, connection=connection)
            workflow = self.workflows[workflow_id]
            result["availableTransitions"] = [
                transition for transition in workflow["transitions"] if row["state"] in transition["from"] or "*" in transition["from"]
            ]
            return result

    def list_records(self, workflow_id: str, state: str | None = None, search: str = "", filters: Mapping[str, Any] | None = None,
                     limit: int = 100, offset: int = 0) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        valid_states = {item["id"] for item in workflow["states"]}
        if state and state not in valid_states:
            raise WorkflowError("Unknown state filter.", 422, "validation_error")
        limit = max(1, min(int(limit), 500)); offset = max(0, int(offset))
        sql = "SELECT * FROM workflow_records WHERE workflow_id=? AND deleted_at IS NULL"
        params: list[Any] = [workflow_id]
        if state:
            sql += " AND state=?"; params.append(state)
        sql += " ORDER BY updated_at DESC,id"
        with self._connect() as connection:
            candidates = [self._row_payload(row) for row in connection.execute(sql, params)]
        search_lower = search.strip().lower()
        filters = dict(filters or {})
        def matches(record: dict[str, Any]) -> bool:
            values = record["values"]
            if search_lower and search_lower not in _canonical_json(values).lower() and search_lower not in record["state"].lower():
                return False
            return all(str(values.get(key, "")).lower() == str(value).lower() for key, value in filters.items())
        matched = [record for record in candidates if matches(record)]
        return {"items": matched[offset:offset + limit], "total": len(matched), "limit": limit, "offset": offset}

    def update(self, workflow_id: str, record_id: str, supplied: Any, expected_version: Any, actor: Any = None) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        actor, version = self.validate_actor(actor), self._expected_version(expected_version)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._active_record(connection, workflow_id, record_id)
            if row["version"] != version:
                raise WorkflowError("Record was changed by another user.", 409, "version_conflict", {"currentVersion": row["version"]})
            before = json.loads(row["data_json"])
            values = self._validate_values(workflow, supplied, creating=False, existing=before)
            changed = {key: {"from": before.get(key), "to": value} for key, value in values.items() if before.get(key) != value}
            if not changed:
                return self._row_payload(row)
            new_version, now = version + 1, _now()
            cursor = connection.execute(
                "UPDATE workflow_records SET data_json=?,version=?,updated_at=? WHERE id=? AND version=? AND deleted_at IS NULL",
                (_canonical_json(values), new_version, now, record_id, version),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("Record was changed by another user.", 409, "version_conflict")
            self._append_event(connection, workflow_id, record_id, "updated", None, row["state"], row["state"], actor, new_version, {"changes": changed})
            self._sync_resources(connection, workflow_id, record_id)
            updated = self._active_record(connection, workflow_id, record_id)
        return self._row_payload(updated)

    def transition(self, workflow_id: str, record_id: str, transition_id: str, expected_version: Any, actor: Any = None,
                   metadata: Any = None) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        transition = next((item for item in workflow["transitions"] if item["id"] == transition_id), None)
        if not transition:
            raise WorkflowError("Transition not found.", 404, "transition_not_found")
        actor, version = self.validate_actor(actor), self._expected_version(expected_version)
        if metadata is not None and not isinstance(metadata, dict):
            raise WorkflowError("Transition metadata must be an object.", 422, "validation_error")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._active_record(connection, workflow_id, record_id)
            if row["version"] != version:
                raise WorkflowError("Record was changed by another user.", 409, "version_conflict", {"currentVersion": row["version"]})
            if row["state"] not in transition["from"] and "*" not in transition["from"]:
                raise WorkflowError(f"Transition {transition_id} is not allowed from {row['state']}.", 409, "invalid_transition")
            new_version, now = version + 1, _now()
            connection.execute(
                "UPDATE workflow_records SET state=?,version=?,updated_at=? WHERE id=? AND version=?",
                (transition["to"], new_version, now, record_id, version),
            )
            self._append_event(connection, workflow_id, record_id, "transitioned", transition_id, row["state"], transition["to"], actor, new_version, {"metadata": metadata or {}})
            self._sync_resources(connection, workflow_id, record_id)
            updated = self._active_record(connection, workflow_id, record_id)
        return self._row_payload(updated)

    def delete(self, workflow_id: str, record_id: str, expected_version: Any, actor: Any = None) -> None:
        self._workflow(workflow_id)
        actor, version = self.validate_actor(actor), self._expected_version(expected_version)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._active_record(connection, workflow_id, record_id)
            if row["version"] != version:
                raise WorkflowError("Record was changed by another user.", 409, "version_conflict", {"currentVersion": row["version"]})
            new_version, now = version + 1, _now()
            connection.execute("UPDATE workflow_records SET version=?,updated_at=?,deleted_at=? WHERE id=? AND version=?", (new_version, now, now, record_id, version))
            self._append_event(connection, workflow_id, record_id, "deleted", None, row["state"], None, actor, new_version, {})
            self._sync_resources(connection, workflow_id, record_id, deleted=True)

    @staticmethod
    def _expected_version(value: Any) -> int:
        if isinstance(value, str):
            value = value.strip().strip('"').removeprefix("W/").strip('"')
        try:
            version = int(value)
        except (TypeError, ValueError):
            raise WorkflowError("A numeric record version is required.", 428, "version_required")
        if version < 1:
            raise WorkflowError("A positive record version is required.", 428, "version_required")
        return version

    def events(self, workflow_id: str, record_id: str, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        self._workflow(workflow_id)
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            records = connection.execute(
                "SELECT * FROM workflow_events WHERE workflow_id=? AND record_id=? ORDER BY id", (workflow_id, record_id)
            ).fetchall()
            return [{
                "id": row["id"], "workflowId": row["workflow_id"], "recordId": row["record_id"],
                "eventType": row["event_type"], "transitionId": row["transition_id"],
                "fromState": row["from_state"], "toState": row["to_state"], "actor": row["actor"],
                "version": row["version"], "payload": json.loads(row["payload_json"]),
                "previousHash": row["previous_hash"], "eventHash": row["event_hash"], "createdAt": row["created_at"],
            } for row in records]
        finally:
            if owns_connection:
                connection.close()

    def dashboard(self) -> dict[str, Any]:
        result = []
        total = 0
        with self._connect() as connection:
            for workflow in self.workflows.values():
                grouped = {row["state"]: row["count"] for row in connection.execute(
                    "SELECT state,COUNT(*) count FROM workflow_records WHERE workflow_id=? AND deleted_at IS NULL GROUP BY state", (workflow["id"],)
                )}
                count = sum(grouped.values()); total += count
                result.append({"id": workflow["id"], "title": workflow["title"], "total": count, "byState": grouped})
        return {"totalRecords": total, "workflowCount": len(self.workflows), "workflows": result}

    def verify_audit_chain(self, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Verify ordering, linkage, and SHA-256 content hashes for every event."""
        owns_connection = connection is None
        connection = connection or self._connect()
        invalid: list[int] = []
        previous_by_record: dict[tuple[str, str], str | None] = {}
        count = 0
        try:
            for row in connection.execute("SELECT * FROM workflow_events ORDER BY workflow_id,record_id,id"):
                count += 1
                key = (row["workflow_id"], row["record_id"])
                expected_previous = previous_by_record.get(key)
                event_material = {
                    "workflowId": row["workflow_id"], "recordId": row["record_id"], "eventType": row["event_type"],
                    "transitionId": row["transition_id"], "fromState": row["from_state"], "toState": row["to_state"],
                    "actor": row["actor"], "version": row["version"], "payload": json.loads(row["payload_json"]),
                    "previousHash": row["previous_hash"], "createdAt": row["created_at"],
                }
                expected_hash = hashlib.sha256(_canonical_json(event_material).encode()).hexdigest()
                if row["previous_hash"] != expected_previous or row["event_hash"] != expected_hash:
                    invalid.append(row["id"])
                previous_by_record[key] = row["event_hash"]
            return {"valid": not invalid, "eventCount": count, "invalidEventIds": invalid}
        finally:
            if owns_connection:
                connection.close()

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            event_count = connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]
            audit = self.verify_audit_chain(connection)
            resource_status = []
            for resource in self.resources:
                found = connection.execute("SELECT type FROM sqlite_master WHERE lower(name)=lower(?)", (resource["name"],)).fetchone()
                registry = connection.execute("SELECT runtime_owned FROM workflow_resource_registry WHERE name=?", (resource["name"],)).fetchone()
                count = connection.execute(f"SELECT COUNT(*) FROM {_quoted(resource['name'])}").fetchone()[0] if found else 0
                resource_status.append({**resource, "ready": bool(found), "actualType": found["type"] if found else None,
                                        "runtimeOwned": bool(registry and registry["runtime_owned"]), "rowCount": count})
        return {
            "status": "ok" if integrity == "ok" else "degraded", "configured": self.config_path.is_file(),
            "configVersion": self.config["version"], "configHash": self.config_hash,
            "database": {"integrity": integrity, "path": str(self.database_path)},
            "workflowCount": len(self.workflows), "eventCount": event_count, "audit": audit,
            "resources": resource_status, "dashboard": self.dashboard(), "timestamp": _now(),
        }

    def export_csv(self, workflow_id: str) -> str:
        workflow = self._workflow(workflow_id)
        records = self.list_records(workflow_id, limit=500)["items"]
        output = io.StringIO(newline="")
        field_keys = [field["key"] for field in workflow["fields"]]
        writer = csv.DictWriter(output, fieldnames=["id", "state", "version", "createdAt", "updatedAt", *field_keys])
        writer.writeheader()
        for record in records:
            values = {key: record["values"].get(key, "") for key in field_keys}
            for key, value in values.items():
                if isinstance(value, (dict, list)):
                    values[key] = _canonical_json(value)
            writer.writerow({"id": record["id"], "state": record["state"], "version": record["version"],
                             "createdAt": record["createdAt"], "updatedAt": record["updatedAt"], **values})
        return output.getvalue()

    def dispatch(self, method: str, path: str, query: Mapping[str, list[str]] | None = None,
                 payload: Any = None, headers: Mapping[str, str] | None = None) -> Response | None:
        """Map the stable HTTP contract to engine calls; return None for legacy routes."""
        query, headers = query or {}, headers or {}
        method = method.upper()
        actor = headers.get("X-Actor") or headers.get("x-actor") or "local-admin"
        if path in {"/api/product/status", "/api/workflows/status", "/api/workflows/health"} and method == "GET":
            return Response(200, self.status())
        if path in {"/api/product/dashboard", "/api/workflows/dashboard"} and method == "GET":
            return Response(200, self.dashboard())
        if path == "/api/workflows" and method == "GET":
            definitions = self.definitions()
            return Response(200, {"workflows": definitions, "items": definitions, "total": len(self.workflows)})
        match = re.fullmatch(r"/api/workflows/([^/]+)/export\.csv", path)
        if match and method == "GET":
            workflow_id = unquote(match.group(1)); body = self.export_csv(workflow_id)
            return Response(200, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{workflow_id}.csv"'})
        match = re.fullmatch(r"/api/workflows/([^/]+)/records", path)
        if match:
            workflow_id = unquote(match.group(1))
            if method == "GET":
                reserved = {"state", "q", "limit", "offset"}
                filters = {key[6:]: values[0] for key, values in query.items() if key.startswith("field.") and values}
                result = self.list_records(workflow_id, query.get("state", [None])[0], query.get("q", [""])[0], filters,
                                           query.get("limit", [100])[0], query.get("offset", [0])[0])
                result["records"] = result["items"]
                return Response(200, result)
            if method == "POST":
                body = payload or {}; values = body.get("values", body.get("data", body)) if isinstance(body, dict) else body
                if isinstance(values, dict): values = {key: value for key, value in values.items() if key not in {"version", "expectedVersion", "_actor"}}
                result = self.create(workflow_id, values, actor)
                return Response(201, {**result, "record": result}, headers={"Location": f'/api/workflows/{workflow_id}/records/{result["id"]}'})
        match = re.fullmatch(r"/api/workflows/([^/]+)/records/([^/]+)/events", path)
        if match and method == "GET":
            events = self.events(unquote(match.group(1)), unquote(match.group(2)))
            return Response(200, {"events": events, "items": events})
        match = re.fullmatch(r"/api/workflows/([^/]+)/records/([^/]+)/transitions/([^/]+)", path)
        if match and method == "POST":
            body = payload or {}
            version = body.get("version", body.get("expectedVersion", headers.get("If-Match") or headers.get("if-match"))) if isinstance(body, dict) else None
            result = self.transition(unquote(match.group(1)), unquote(match.group(2)), unquote(match.group(3)), version, actor,
                                     body.get("metadata") if isinstance(body, dict) else None)
            return Response(200, {**result, "record": result}, headers={"ETag": f'"{result["version"]}"'})
        match = re.fullmatch(r"/api/workflows/([^/]+)/records/([^/]+)", path)
        if match:
            workflow_id, record_id = unquote(match.group(1)), unquote(match.group(2))
            if method == "GET":
                result = self.get(workflow_id, record_id)
                return Response(200, {**result, "record": result}, headers={"ETag": f'"{result["version"]}"'})
            if method == "PATCH":
                body = payload or {}
                version = body.get("version", body.get("expectedVersion", headers.get("If-Match") or headers.get("if-match"))) if isinstance(body, dict) else None
                values = body.get("values", body.get("data")) if isinstance(body, dict) else None
                if values is None and isinstance(body, dict):
                    values = {key: value for key, value in body.items() if key not in {"version", "expectedVersion", "_actor"}}
                result = self.update(workflow_id, record_id, values, version, actor)
                return Response(200, {**result, "record": result}, headers={"ETag": f'"{result["version"]}"'})
            if method == "DELETE":
                version = query.get("version", [None])[0] or headers.get("If-Match") or headers.get("if-match")
                self.delete(workflow_id, record_id, version, actor)
                return Response(204, None)
        if path.startswith("/api/workflows/") or path.startswith("/api/product/"):
            raise WorkflowError("Endpoint not found.", 404, "not_found")
        return None


def render_workflow_ui(title: str, selected_workflow: str = "") -> str:
    """Return the standalone, API-backed product workspace."""
    safe_title = html.escape(title)
    initial = json.dumps(selected_workflow)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} · Operations</title><style>
:root{{--ink:#132238;--muted:#65758b;--line:#d9e2ec;--brand:#086b7a;--side:#102a43;--bg:#f4f7fa;--ok:#13795b;--danger:#b42318}}*{{box-sizing:border-box}}body{{margin:0;font:14px system-ui;color:var(--ink);background:var(--bg)}}button,input,select,textarea{{font:inherit}}.app{{display:grid;grid-template-columns:280px 1fr;min-height:100vh}}aside{{background:var(--side);color:white;padding:24px 16px}}aside h1{{font-size:20px;margin:0 8px 5px}}aside p{{color:#b8c8d8;margin:0 8px 22px}}#workflow-nav button{{display:block;width:100%;text-align:left;border:0;color:#e7eef5;background:transparent;padding:11px;border-radius:8px;cursor:pointer}}#workflow-nav button.active{{background:#d7f0f4;color:#083344;font-weight:800}}main{{padding:32px;min-width:0}}.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}}h2{{font-size:30px;margin:4px 0}}.muted{{color:var(--muted)}}.primary,.transition{{border:0;background:var(--brand);color:white;border-radius:7px;padding:10px 15px;font-weight:800;cursor:pointer}}.danger{{border:1px solid #efb5af;background:white;color:var(--danger);border-radius:7px;padding:9px 14px;cursor:pointer}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:22px 0}}.metric{{background:white;border:1px solid var(--line);padding:16px;border-radius:10px}}.metric strong{{display:block;font-size:24px}}.toolbar{{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}}.toolbar input,.toolbar select{{padding:10px;border:1px solid var(--line);border-radius:7px;background:white}}.table{{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;min-width:700px}}th,td{{padding:12px;border-bottom:1px solid var(--line);text-align:left}}th{{font-size:11px;text-transform:uppercase;background:#edf2f7}}tbody tr{{cursor:pointer}}tbody tr:hover{{background:#eefbfc}}.badge{{display:inline-block;padding:4px 8px;background:#e7f5f2;color:#12624d;border-radius:99px;font-size:12px;font-weight:800}}dialog{{border:0;border-radius:14px;padding:0;width:min(760px,calc(100% - 30px));box-shadow:0 30px 90px #0005}}dialog::backdrop{{background:#071827aa}}.dialog-head{{display:flex;justify-content:space-between;align-items:center;padding:20px 24px;border-bottom:1px solid var(--line)}}.dialog-head h3{{margin:0;font-size:21px}}.close{{border:0;background:#edf2f7;border-radius:50%;width:34px;height:34px;font-size:20px;cursor:pointer}}.dialog-body{{padding:22px 24px;max-height:70vh;overflow:auto}}.fields{{display:grid;grid-template-columns:repeat(2,1fr);gap:15px}}label{{display:grid;gap:6px;font-weight:700}}label.full{{grid-column:1/-1}}input,select,textarea{{width:100%;padding:10px;border:1px solid #bcccdc;border-radius:7px}}textarea{{min-height:100px}}.dialog-actions,.transition-row{{display:flex;gap:9px;flex-wrap:wrap;margin-top:18px}}.history{{margin-top:25px;padding-top:15px;border-top:1px solid var(--line)}}.event{{border-left:3px solid #9ecbd1;padding:5px 12px;margin:10px 0}}.error{{color:var(--danger);font-weight:700}}@media(max-width:760px){{.app{{grid-template-columns:1fr}}aside{{padding:15px}}main{{padding:20px}}.fields{{grid-template-columns:1fr}}}}
</style></head><body><div class="app"><aside><h1>{safe_title}</h1><p>Operations workspace</p><nav id="workflow-nav"></nav><p><a href="/" style="color:#b8c8d8">Feature catalog →</a></p></aside><main><div id="workspace">Loading workflows…</div></main></div>
<dialog id="editor"><header class="dialog-head"><h3 id="editor-title">Record</h3><button class="close" type="button">×</button></header><div class="dialog-body"><form id="record-form"><div id="record-fields" class="fields"></div><p id="form-error" class="error"></p><div class="dialog-actions"><button class="primary" type="submit">Save record</button><button id="delete-record" class="danger" type="button">Delete</button></div></form><div id="transitions" class="transition-row"></div><section id="history" class="history"></section></div></dialog>
<script>(()=>{{const initial={initial};let workflows=[],current=null,records=[],editing=null;const nav=document.querySelector('#workflow-nav'),workspace=document.querySelector('#workspace'),dialog=document.querySelector('#editor'),form=document.querySelector('#record-form');const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));const api=async(url,options={{}})=>{{options.headers={{'Content-Type':'application/json','X-Actor':'local-admin',...(options.headers||{{}})}};const response=await fetch(url,options);if(response.status===204)return null;const body=await response.json();if(!response.ok)throw new Error(body.error||'Request failed');return body;}};
const fieldInput=(field,value='')=>{{const required=field.required?' required':'';let control;if(field.type==='textarea')control=`<textarea name="${{field.key}}"${{required}}>${{esc(value)}}</textarea>`;else if(field.type==='boolean')control=`<select name="${{field.key}}"><option value="false" ${{value===false?'selected':''}}>No</option><option value="true" ${{value===true?'selected':''}}>Yes</option></select>`;else if(field.type==='select')control=`<select name="${{field.key}}"${{required}}><option value="">Choose…</option>${{field.options.map(o=>{{const ov=typeof o==='object'?o.value:o,ol=typeof o==='object'?(o.label||o.value):o;return `<option value="${{esc(ov)}}" ${{String(ov)===String(value)?'selected':''}}>${{esc(ol)}}</option>`}}).join('')}}</select>`;else control=`<input name="${{field.key}}" type="${{field.type==='number'||field.type==='integer'?'number':field.type==='date'?'date':field.type==='datetime'?'datetime-local':field.type==='email'?'email':field.type==='url'?'url':'text'}}" value="${{esc(value)}}"${{required}}>`;return `<label class="${{field.type==='textarea'?'full':''}}">${{esc(field.label)}}${{control}}</label>`}};
const render=async()=>{{if(!current){{workspace.innerHTML='<h2>No product workflows configured</h2>';return}};const data=await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records`);records=data.items;const cols=current.fields.slice(0,4);workspace.innerHTML=`<div class="top"><div><div class="muted">Product workflow</div><h2>${{esc(current.title)}}</h2><p class="muted">${{esc(current.description)}}</p></div><button id="new-record" class="primary">New record</button></div><div class="metrics"><div class="metric"><span class="muted">Active records</span><strong>${{data.total}}</strong></div>${{current.states.map(s=>`<div class="metric"><span class="muted">${{esc(s.label)}}</span><strong>${{records.filter(r=>r.state===s.id).length}}</strong></div>`).join('')}}</div><div class="toolbar"><input id="search" placeholder="Search records"><select id="state-filter"><option value="">All states</option>${{current.states.map(s=>`<option value="${{s.id}}">${{esc(s.label)}}</option>`).join('')}}</select><a href="/api/workflows/${{encodeURIComponent(current.id)}}/export.csv">Export CSV</a></div><div class="table"><table><thead><tr><th>State</th>${{cols.map(f=>`<th>${{esc(f.label)}}</th>`).join('')}}<th>Updated</th></tr></thead><tbody id="record-rows"></tbody></table></div>`;drawRows(records,cols);document.querySelector('#new-record').onclick=()=>openEditor();document.querySelector('#search').oninput=filter;document.querySelector('#state-filter').onchange=filter;}};
const drawRows=(items,cols)=>{{document.querySelector('#record-rows').innerHTML=items.map(r=>`<tr data-id="${{r.id}}"><td><span class="badge">${{esc(r.state)}}</span></td>${{cols.map(f=>`<td>${{esc(r.values[f.key])}}</td>`).join('')}}<td>${{new Date(r.updatedAt).toLocaleString()}}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">No records yet.</td></tr>';document.querySelectorAll('tr[data-id]').forEach(row=>row.onclick=()=>openEditor(row.dataset.id));}};const filter=()=>{{const q=document.querySelector('#search').value.toLowerCase(),state=document.querySelector('#state-filter').value;drawRows(records.filter(r=>(!state||r.state===state)&&(!q||JSON.stringify(r.values).toLowerCase().includes(q))),current.fields.slice(0,4));}};
const openEditor=async id=>{{editing=id?await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records/${{id}}`):null;document.querySelector('#editor-title').textContent=editing?'Edit record':'New record';document.querySelector('#record-fields').innerHTML=current.fields.map(f=>fieldInput(f,editing?.values[f.key]??f.default??f.defaultValue??'')).join('');document.querySelector('#delete-record').hidden=!editing;document.querySelector('#form-error').textContent='';document.querySelector('#transitions').innerHTML=editing?editing.availableTransitions.map(t=>`<button type="button" class="transition" data-transition="${{t.id}}">${{esc(t.label)}}</button>`).join(''):'';document.querySelectorAll('[data-transition]').forEach(b=>b.onclick=async()=>{{await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records/${{editing.id}}/transitions/${{b.dataset.transition}}`,{{method:'POST',body:JSON.stringify({{version:editing.version}})}});dialog.close();await render();}});document.querySelector('#history').innerHTML=editing?`<h3>Audit history</h3>${{editing.events.slice().reverse().map(e=>`<div class="event"><strong>${{esc(e.eventType)}}</strong> · ${{esc(e.actor)}}<br><small>${{new Date(e.createdAt).toLocaleString()}} · version ${{e.version}}</small></div>`).join('')}}`:'';dialog.showModal();}};
form.onsubmit=async event=>{{event.preventDefault();const values={{}};for(const field of current.fields){{const element=form.elements[field.key];let value=element.value;if(field.type==='number')value=value===''?null:Number(value);if(field.type==='integer')value=value===''?null:parseInt(value,10);if(field.type==='boolean')value=value==='true';values[field.key]=value;}}try{{if(editing)await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records/${{editing.id}}`,{{method:'PATCH',body:JSON.stringify({{version:editing.version,values}})}});else await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records`,{{method:'POST',body:JSON.stringify({{values}})}});dialog.close();await render();}}catch(error){{document.querySelector('#form-error').textContent=error.message;}}}};document.querySelector('#delete-record').onclick=async()=>{{if(editing&&confirm('Delete this record?')){{await api(`/api/workflows/${{encodeURIComponent(current.id)}}/records/${{editing.id}}?version=${{editing.version}}`,{{method:'DELETE'}});dialog.close();await render();}}}};dialog.querySelector('.close').onclick=()=>dialog.close();
api('/api/workflows').then(data=>{{workflows=data.items;current=workflows.find(w=>w.id===initial)||workflows[0];nav.innerHTML=workflows.map(w=>`<button data-workflow="${{w.id}}" class="${{current&&w.id===current.id?'active':''}}">${{esc(w.title)}}<br><small>${{w.metrics.total||0}} records</small></button>`).join('');nav.querySelectorAll('button').forEach(button=>button.onclick=()=>{{current=workflows.find(w=>w.id===button.dataset.workflow);nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b===button));history.replaceState(null,'',`/workflows/${{current.id}}`);render();}});render();}}).catch(error=>workspace.innerHTML=`<p class="error">${{esc(error.message)}}</p>`);}})();</script></body></html>'''
