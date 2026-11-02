"""Local structured span logs and OTLP/HTTP JSON export (Langfuse or another collector).

Telemetry is optional. Only explicit scalar attributes are recorded: never prompts, responses,
commands, environment values or exception messages. Export failure cannot change ticket state.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

_current: ContextVar = ContextVar("devflow_span", default=None)
_settings: dict = {}
_pending: list[dict] = []
# Durable diagnostics are scoped to the configured repo's ignored state directory.


def configure(root: Path | None = None, *, settings: dict | None = None):
    global _settings
    flush()
    if settings is None:
        explicit = os.getenv("DEVFLOW_TELEMETRY_CONFIG")
        path = Path(explicit) if explicit else (root / ".devflow/state/telemetry.json" if root else None)
        try:
            settings = json.loads(path.read_text()) if path and path.exists() else {}
        except (OSError, ValueError, TypeError):
            print("devflow: invalid telemetry config; tracing disabled", file=sys.stderr)
            settings = {}
    _settings = dict(settings) if isinstance(settings, dict) else {}
    _settings.setdefault("enabled", bool(_settings.get("endpoint") or _settings.get("log_file")))


@dataclass
class Span:
    name: str
    attributes: dict = field(default_factory=dict)
    trace_id: str = ""
    span_id: str = ""
    parent_id: str = ""
    start_ns: int = 0
    status: str = "ok"

    def set(self, **values):
        for key, value in values.items():
            if isinstance(value, str | int | float | bool) and value is not None:
                self.attributes[key] = value[:500] if isinstance(value, str) else value

    def fail(self):
        self.status = "error"


@contextmanager
def span(name: str, attributes: dict | None = None):
    parent = _current.get()
    inherited = {k: v for k, v in (parent.attributes if parent else {}).items()
                 if k.startswith("langfuse.trace.") or k == "langfuse.session.id"}
    item = Span(name=name, attributes=inherited)
    item.set(**(attributes or {}))
    item.trace_id = parent.trace_id if parent else secrets.token_hex(16)
    item.span_id = secrets.token_hex(8)
    item.parent_id = parent.span_id if parent else ""
    item.start_ns = time.time_ns()
    token = _current.set(item)
    try:
        yield item
    except BaseException as exc:
        # A human gate is a normal pause, not a failed model or tool call.
        item.status = "waiting" if type(exc).__name__ == "_Wait" else "error"
        item.set(**{"error.type": type(exc).__name__})
        raise
    finally:
        _current.reset(token)
        if _settings.get("enabled"):
            record = {"name": item.name, "trace_id": item.trace_id, "span_id": item.span_id,
                      "parent_id": item.parent_id, "start_ns": item.start_ns,
                      "end_ns": time.time_ns(), "status": item.status,
                      "attributes": item.attributes}
            try:
                path = _settings.get("log_file")
                if path:
                    target = Path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("a") as log:
                        log.write(json.dumps(record) + "\n")
                    target.chmod(0o600)
                _pending.append(record)
                if len(_pending) >= 100:
                    flush()
            except (OSError, ValueError, TypeError):
                print("devflow: telemetry log unavailable", file=sys.stderr)


def event(name: str, **attributes):
    with span(name, attributes):
        pass


def _attribute(key, value):
    kind = "boolValue" if isinstance(value, bool) else "intValue" if isinstance(value, int) else "doubleValue" if isinstance(value, float) else "stringValue"
    return {"key": key, "value": {kind: str(value) if kind == "intValue" else value}}


def export(records: list[dict], settings: dict | None = None):
    config = settings if settings is not None else _settings
    if not config.get("endpoint") or not records:
        return
    observations = []
    for row in records:
        attributes = dict(row["attributes"])
        attributes["devflow.outcome"] = row["status"]
        observations.append({
            "traceId": row["trace_id"], "spanId": row["span_id"],
            "parentSpanId": row["parent_id"], "name": row["name"], "kind": 1,
            "startTimeUnixNano": str(row["start_ns"]), "endTimeUnixNano": str(row["end_ns"]),
            "attributes": [_attribute(k, v) for k, v in attributes.items()],
            "status": {"code": 2 if row["status"] == "error" else 1},
        })
    payload = {"resourceSpans": [{"resource": {"attributes": [
        _attribute("service.name", "devflow"), _attribute("service.version", "0.1.0")
    ]}, "scopeSpans": [{"scope": {"name": "devflow"}, "spans": observations}]}]}
    headers = {"Content-Type": "application/json", "x-langfuse-ingestion-version": "4"}
    if config.get("public_key") and config.get("secret_key"):
        auth = base64.b64encode(f"{config['public_key']}:{config['secret_key']}".encode()).decode()
        headers["Authorization"] = "Basic " + auth
    request = urllib.request.Request(config["endpoint"], data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        # OTLP may acknowledge a batch but reject individual spans.
        result = json.loads(response.read() or b"{}")
        if int(result.get("partialSuccess", {}).get("rejectedSpans", 0)):
            raise ValueError("collector rejected spans")


def flush():
    batch, _pending[:] = list(_pending), []
    try:
        export(batch)
    except (OSError, ValueError, TypeError):
        print("devflow: trace export unavailable; local trace log retained", file=sys.stderr)


def ticket_run(function):
    @wraps(function)
    def wrapped(self, issue):
        repo = self.cfg.name
        with span("ticket.run", {
            "langfuse.trace.name": "devflow.ticket", "langfuse.session.id": f"{repo}:issue-{issue}",
            "langfuse.trace.metadata.repo": repo, "langfuse.trace.metadata.issue": str(issue),
            "devflow.issue": issue,
        }) as trace:
            result = function(self, issue)
            trace.set(**{"devflow.state": result.ticket.state.value, "devflow.result": result.status})
            if result.status == "paused":
                trace.status = "waiting"
            if result.status == "escalated":
                trace.fail()
            return result
    return wrapped


def agent_call(function):
    @wraps(function)
    def wrapped(self, req):
        with span("agent.call", {"devflow.agent": self.name, "devflow.role": req.role,
                                  "langfuse.observation.type": "generation",
                                  "devflow.input_characters": len(req.system) + len(req.prompt),
                                  "devflow.resumed": bool(req.session)}) as call:
            result = function(self, req)
            usage = result.raw.get("usage", {})
            usage = usage if isinstance(usage, dict) else {}
            call.set(**{"devflow.output_characters": len(result.text), "devflow.ok": result.ok})
            counts = {}
            for kind in ("input", "output"):
                value = usage.get(f"{kind}_tokens")
                if isinstance(value, int | float) and not isinstance(value, bool):
                    call.set(**{f"gen_ai.usage.{kind}_tokens": value})
                    counts[kind] = value
            if counts:
                call.set(**{"langfuse.observation.usage_details": json.dumps(counts)})
            if result.cost_usd is not None:
                call.set(**{"langfuse.observation.cost_details": json.dumps({"total": result.cost_usd}),
                             "devflow.cost_source": "provider_reported"})
            if req.model:
                call.set(**{"gen_ai.request.model": req.model})
            if not result.ok:
                call.fail()
            return result
    return wrapped


def operation(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
