"""Local structured span logs and OTLP/HTTP JSON export (Langfuse or another collector).

Telemetry is optional. Detailed content capture is opt-in per repository, redacted and bounded.
Export failure cannot change ticket state. Child CLI environment values are never recorded.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.request
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

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

    def content(self, *, input=None, output=None):
        if not detailed():
            return
        for key, value in (("input", input), ("output", output)):
            if value is None:
                continue
            try:
                old = self.attributes.get(f"langfuse.observation.{key}")
                if old and isinstance(value, dict):
                    previous = json.loads(old)
                    if isinstance(previous, dict):
                        value = {**previous, **value}
                text = json.dumps(redact(value), ensure_ascii=False, default=str)
                limit = max(1000, min(int(_settings.get("max_content_characters", 200_000)), 1_000_000))
                if len(text) > limit:
                    original = len(text)
                    text = json.dumps({"preview": text[:limit], "truncated": True,
                                       "original_characters": original}, ensure_ascii=False)
                    self.set(**{f"devflow.{key}_truncated": True})
                self.attributes[f"langfuse.observation.{key}"] = text
            except (TypeError, ValueError, RecursionError):
                self.set(**{f"devflow.{key}_capture_error": True})

    def fail(self):
        self.status = "error"


@contextmanager
def span(name: str, attributes: dict | None = None):
    parent = _current.get()
    inherited = {k: v for k, v in (parent.attributes if parent else {}).items()
                 if k.startswith("langfuse.trace.") or k == "langfuse.session.id"}
    item = Span(name=name, attributes=inherited)
    item.set(**(attributes or {}))
    if "langfuse.observation.type" not in item.attributes:
        item.set(**{"langfuse.observation.type": "span"})
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
                    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as log:
                        log.write(json.dumps(record) + "\n")
                    target.chmod(0o600)
                _pending.append(record)
                if len(_pending) >= 100:
                    flush()
            except (OSError, ValueError, TypeError):
                print("devflow: telemetry log unavailable", file=sys.stderr)


def event(name: str, **attributes):
    with span(name, {"langfuse.observation.type": "event", **attributes}):
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


_root: ContextVar = ContextVar("devflow_root_span", default=None)

GRAPH_STATES = ("fresh", "refreshed", "failed", "unavailable")
FINGERPRINT_CHARS = 12
_HEX_SHA = re.compile(r"[0-9a-f]{7,64}")
_HEX = re.compile(r"[0-9a-f]*")
_REMOTE_PART = re.compile(r"[A-Za-z0-9._~-]+")


def sanitize_remote(url) -> str:
    """host/owner/repo from a git remote, with userinfo, port, query and fragment dropped.

    Returns "" when the URL cannot be parsed cleanly, so a partial value is never emitted.
    """
    if not isinstance(url, str) or not url.strip() or len(url) > 500:
        return ""
    text = url.strip()
    try:
        if "://" in text:
            parts = urlsplit(text)
            host, path = parts.hostname or "", parts.path
        else:
            match = re.fullmatch(r"(?:[^@/\s:]+@)?([A-Za-z0-9.\-]+):(?!//)([^?#\s]+)", text)
            if not match:
                return ""
            host, path = match[1], match[2]
    except ValueError:
        return ""
    path = path.strip("/")
    path = path.removesuffix(".git")
    segments = path.split("/")
    if not host or not _REMOTE_PART.fullmatch(host) or len(segments) < 2:
        return ""
    if not all(_REMOTE_PART.fullmatch(s) for s in segments):
        return ""
    return f"{host.lower()}/{'/'.join(segments)}"


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid(key: str, value):
    """Validated value for an allowlisted provenance key, else None (dropped)."""
    if key == "base_sha":
        return value if isinstance(value, str) and _HEX_SHA.fullmatch(value) else None
    if key == "fingerprint":
        if isinstance(value, str) and _HEX.fullmatch(value):
            return value[:FINGERPRINT_CHARS]
        return None
    if key == "status":
        return value if value in GRAPH_STATES else None
    if key in ("nodes", "edges"):
        return value if _count(value) else None
    if key == "refreshed":
        return value if isinstance(value, bool) else None
    if key == "remote":
        return sanitize_remote(value) or None
    return None


def annotate(**attrs) -> None:
    """Set validated graph attributes (refreshed, status, nodes, edges, fingerprint) on the current span."""
    with suppress(Exception):  # telemetry must never affect the workflow
        current = _current.get()
        if current is None or not _settings.get("enabled"):
            return
        for key, value in attrs.items():
            if key not in ("refreshed", "status", "nodes", "edges", "fingerprint"):
                continue
            clean = _valid(key, value)
            if clean is not None:
                current.set(**{f"devflow.graph.{key}": clean})


_TRACE_KEYS = {"base_sha": "base_sha", "remote": "remote", "graph_status": "status",
               "graph_fingerprint": "fingerprint", "graph_nodes": "nodes", "graph_edges": "edges"}


def trace_metadata(**values) -> None:
    """Set allowlisted provenance on the ticket root (and current span, so later children inherit).

    A value of None clears the key. Invalid values and unlisted keys are dropped.
    """
    with suppress(Exception):
        root = _root.get()
        if root is None or not _settings.get("enabled"):
            return
        targets = [root] if _current.get() in (None, root) else [root, _current.get()]
        for key, value in values.items():
            kind = _TRACE_KEYS.get(key)
            if kind is None:
                continue
            clean = None if value is None else _valid(kind, value)
            names = (f"langfuse.trace.metadata.{key}", f"devflow.{key}")
            if value is None:
                for target in targets:
                    for name in names:
                        target.attributes.pop(name, None)
            elif clean is not None:
                for target in targets:
                    target.set(**dict.fromkeys(names, clean))


def ticket_run(function):
    @wraps(function)
    def wrapped(self, issue):
        repo = self.cfg.name
        with span("ticket.run", {
            "langfuse.trace.name": "devflow.ticket", "langfuse.session.id": f"{repo}:issue-{issue}",
            "langfuse.trace.metadata.repo": repo, "langfuse.trace.metadata.issue": str(issue),
            "devflow.issue": issue, "langfuse.observation.type": "agent",
        }) as trace:
            trace.content(input={"repository": repo, "issue": issue})
            token = _root.set(trace)
            try:
                result = function(self, issue)
            finally:
                _root.reset(token)
            trace.content(output={"state": result.ticket.state.value, "status": result.status,
                                  "reason": result.reason if hasattr(result, "reason") else ""})
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
                                  "langfuse.observation.type": "agent",
                                  "langfuse.observation.metadata.agent": self.name,
                                  "langfuse.observation.metadata.role": req.role,
                                  "devflow.input_characters": len(req.system) + len(req.prompt),
                                  "devflow.resumed": bool(req.session),
                                  "devflow.handoff.from": _current.get().name if _current.get() else "external",
                                  "devflow.handoff.to": f"{self.name}:{req.role}"}) as call:
            call.name = f"agent.{self.name}.{req.role}"
            call.content(input={"system": req.system, "prompt": req.prompt, "cwd": str(req.cwd),
                                "schema": req.schema, "read_only": req.read_only,
                                "allowed_tools": req.allowed_tools, "disallowed_tools": req.disallowed_tools,
                                "resume_session": req.session})
            call.set(**{"devflow.prompt_sha256": hashlib.sha256((req.system + "\0" + req.prompt).encode()).hexdigest(),
                        "devflow.visibility": "cli_boundary", "devflow.session": req.session or ""})
            try:
                result = function(self, req)
            except Exception as exc:
                call.content(output={"error_type": type(exc).__name__, "error": str(exc)})
                raise
            call.content(output={"response": result.text, "ok": result.ok, "error": result.error,
                                 "session": result.session})
            call.set(**{"devflow.session": result.session or ""})
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
            actual_model = result.raw.get("model")
            if isinstance(actual_model, str):
                call.set(**{"devflow.reported_model": actual_model})
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
            kind = "tool" if name.startswith("tool.") else "retriever" if name == "graph.refresh" else "chain"
            with span(name, {"langfuse.observation.type": kind}) as trace:
                # Whitelist tools, never serialize self/config/credentials or arbitrary function args.
                if detailed() and name == "tool.git":
                    trace.content(input={"cwd": str(args[0]) if args else "", "arguments": args[1:]})
                elif detailed() and name in ("tool.github", "tool.github_project"):
                    trace.content(input={"arguments": args[1:], "stdin": kwargs.get("input_text")})
                elif detailed() and name == "graph.refresh":
                    trace.content(input={"repository": str(args[0]) if args else "", "force": kwargs.get("force", False)})
                result = function(*args, **kwargs)
                if name.startswith("tool.") or name == "graph.refresh":
                    trace.content(output={"result": result})
                return result
        return wrapped
    return decorate


def detailed() -> bool:
    return bool(_settings.get("enabled") and _settings.get("capture_content") is True)


_SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|authorization|api[_-]?key|access[_-]?token|bearer[_-]?token|private[_-]?key|cookie|credential|^token$)")
_SECRET_TEXT = re.compile(r"(?i)(\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|bearer[_-]?token|authorization|cookie)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}{]+)")
_TOKEN = re.compile(r"(?:sk-(?:lf-|ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16}|Bearer\s+[A-Za-z0-9._~+/-]+=*)", re.IGNORECASE)


def redact(value):
    """Mask structured credentials and recognizable secrets before local persistence/export.

    Deliberately does not read arbitrary credential files. Free-form PII is not guaranteed masked.
    """
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        result, hide_next = [], False
        for item in value:
            result.append("[REDACTED]" if hide_next else redact(item))
            hide_next = isinstance(item, str) and item.startswith("-") and bool(_SECRET_KEY.search(item))
        return result
    if isinstance(value, str):
        for key, secret in list(_settings.items()) + list(os.environ.items()):
            if _SECRET_KEY.search(key) and isinstance(secret, str) and len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                       "[REDACTED PRIVATE KEY]", value, flags=re.DOTALL)
        value = re.sub(r"(https?://)[^/\s:@]+:[^@\s/]+@", r"\1[REDACTED]@", value)
        value = _TOKEN.sub("[REDACTED]", value)
        value = re.sub(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie)[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
                       lambda m: m[1] + m[2] + "[REDACTED]" + m[2], value)
        return _SECRET_TEXT.sub(lambda m: m[1] + "[REDACTED]", value)
    return value


def content_event(name, *, input=None, output=None, attributes=None, kind="event"):
    with span(name, {"langfuse.observation.type": kind, **(attributes or {})}) as item:
        item.content(input=input, output=output)
        if (attributes or {}).get("devflow.tool_error"):
            item.fail()


def current_content(*, input=None, output=None):
    current = _current.get()
    if current:
        current.content(input=input, output=output)


def cli_events(text, driver):
    """Record exposed CLI events in order, without inventing internal request timing/context.

    These are reconstructed after process completion. Tool inputs/results are correlated by ID.
    Only documented public message/tool event fields are selected; no raw stream or reasoning dump.
    """
    events = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append(row)
    if not detailed():
        return events
    try:
        _record_cli_events(events, driver)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        print("devflow: unsupported CLI trace event; agent result preserved", file=sys.stderr)
    return events


def _record_cli_events(events, driver):
    tools, generations = {}, {}
    for index, row in enumerate(events):
        attrs = {"devflow.event_index": index, "devflow.reconstructed": True,
                 "devflow.driver": driver, "devflow.timing_precision": "post_process"}
        kind = row.get("type")
        if kind == "system" and row.get("subtype") == "init" and isinstance(row.get("model"), str):
            current = _current.get()
            if current:
                current.set(**{"devflow.reported_model": row["model"]})
        if driver == "codex" and kind == "item.completed":
            item = row.get("item", {})
            item_kind = item.get("type")
            if item_kind in ("command_execution", "mcp_tool_call", "file_change", "web_search", "collab_tool_call"):
                content_event("cli." + item_kind, input={k: item[k] for k in
                              ("command", "arguments", "server", "tool", "changes", "query", "receiver_thread_ids", "prompt") if k in item},
                              output={k: item[k] for k in ("aggregated_output", "result", "error", "status", "exit_code", "agents_states") if k in item},
                              attributes={**attrs, "devflow.tool_call_id": str(item.get("id", ""))},
                              kind="agent" if item_kind == "collab_tool_call" else "tool")
            elif item_kind == "agent_message":
                content_event("cli.assistant_message", output=item.get("text"), attributes=attrs)
        elif driver in ("claude", "cursor"):
            if kind == "assistant":
                message = row.get("message", {})
                if isinstance(message, dict):
                    if driver == "claude" and message.get("id") and message.get("model"):
                        ident = message["id"]
                        entry = generations.setdefault(ident, {"model": message["model"], "usage": {}, "content": [], "index": index})
                        entry["usage"].update(message.get("usage") or {})
                        entry["content"].extend({k: b[k] for k in ("type", "text", "id", "name", "input") if k in b}
                                                for b in message.get("content", [])
                                                if isinstance(b, dict) and b.get("type") in ("text", "tool_use"))
                    for block in message.get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tools[block.get("id")] = block
                        elif block.get("type") == "text" and not (driver == "claude" and message.get("model") and message.get("id")):
                            content_event("cli.assistant_message", output=block.get("text"), attributes=attrs)
            elif kind == "user":
                message = row.get("message", {})
                for block in message.get("content", []) if isinstance(message, dict) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    ident = block.get("tool_use_id")
                    tool = tools.pop(ident, {})
                    content_event("cli.tool." + str(tool.get("name", "unknown")), input=tool.get("input"),
                                  output=block.get("content"), attributes={**attrs, "devflow.tool_call_id": str(ident),
                                  "devflow.tool_error": bool(block.get("is_error"))},
                                  kind="agent" if tool.get("name") in ("Agent", "Task") else "tool")
            elif kind == "tool_call" and row.get("subtype") == "started":
                tools[row.get("call_id")] = row
            elif kind == "tool_call" and row.get("subtype") == "completed":
                ident = row.get("call_id")
                start = tools.pop(ident, {})
                details = row.get("tool_call") or {}
                tool_name = next(iter(details), "unknown") if isinstance(details, dict) else "unknown"
                content_event("cli.cursor_tool." + tool_name, input=start.get("tool_call"), output=details,
                              attributes={**attrs, "devflow.tool_call_id": str(ident)}, kind="tool")
    for ident, generation in generations.items():
        usage = generation["usage"]
        counts = {key: usage[source] for key, source in
                  (("input", "input_tokens"), ("output", "output_tokens"),
                   ("cache_read_input_tokens", "cache_read_input_tokens"),
                   ("cache_creation_input_tokens", "cache_creation_input_tokens"))
                  if isinstance(usage.get(source), (int, float))}
        content_event("cli.claude.generation", output=generation["content"], kind="generation",
                      attributes={"langfuse.observation.model.name": generation["model"],
                                  "langfuse.observation.usage_details": json.dumps(counts),
                                  "devflow.message_id": str(ident), "devflow.reconstructed": True,
                                  "devflow.event_index": generation["index"],
                                  "devflow.timing_precision": "post_process",
                                  "devflow.input_visibility": "CLI does not expose full provider request; see parent agent input and sibling tool results"})
    for ident, tool in tools.items():
        content_event("cli.tool.unfinished", input=tool.get("input", tool.get("tool_call")),
                      attributes={"devflow.tool_call_id": str(ident), "devflow.reconstructed": True}, kind="tool")
