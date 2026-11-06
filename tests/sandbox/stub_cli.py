"""Stand-ins for the real `claude`, `codex`, `openclaw` and `gh` executables.

Invoked as `stub_cli.py <tool> <sandbox-dir> <args...>` by tiny wrapper scripts on PATH. They speak
the same wire formats the real CLIs do (the formats devflow's drivers parse), driven by
`scenario.json`, and keep GitHub state in `gh_state.json`, so the real drivers, subprocess handling,
git, worktrees, gates and engine all run unmodified. Every call is appended to `calls.jsonl`.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

TOOL, SANDBOX, ARGS = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
CALLS, SCENARIO, CURSOR, GH = (SANDBOX / n for n in
                               ("calls.jsonl", "scenario.json", "cursor.json", "gh_state.json"))


def load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def log(**entry) -> None:
    with CALLS.open("a") as fh:
        fh.write(json.dumps({"tool": TOOL, **entry}) + "\n")


def next_action(role: str) -> dict | None:
    """Pop the next scripted action for this tool and role; `sticky` actions repeat forever."""
    queue = load(SCENARIO, {}).get(TOOL, {}).get(role, [])
    cursor = load(CURSOR, {})
    key = f"{TOOL}:{role}"
    index = cursor.get(key, 0)
    if index >= len(queue):
        return None
    action = queue[index]
    if not action.get("sticky"):
        cursor[key] = index + 1
        CURSOR.write_text(json.dumps(cursor))
    return action


def schema_kind(text: str) -> str:
    """Which structured reply the engine asked for, read from the JSON Schema it embedded."""
    schema = None
    for line in text.splitlines():
        if line.startswith('{"$schema"'):
            try:
                schema = json.loads(line)
            except ValueError:
                pass
            break
    if schema is None:
        return ""
    keys = set(schema.get("properties", {}))
    for kind, marker in (("test_plan", "uncovered_criteria"), ("plan", "acceptance_criteria"),
                         ("review", "verdict"), ("lesson", "lessons"), ("triage", "message_to_coder"),
                         ("plan_review", "decision")):
        if marker in keys:
            return kind
    return ""


def apply_writes(action: dict, cwd: Path) -> None:
    for rel, content in (action.get("write") or {}).items():
        target = cwd / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def reply_text(action: dict) -> str:
    reply = action.get("reply", "")
    return reply if isinstance(reply, str) else json.dumps(reply)


def fail(action: dict) -> None:
    message = action["fail"] if isinstance(action.get("fail"), str) else action.get("stderr", "stub failure")
    sys.stderr.write(message + "\n")
    sys.exit(action.get("code", 1))


def value_after(flag: str, default: str = "") -> str:
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS else default


# ------------------------------------------------------------------ claude

def claude() -> None:
    prompt = sys.stdin.read()
    system_file = value_after("--append-system-prompt-file")
    system = Path(system_file).read_text() if system_file else ""
    tools = value_after("--allowedTools").split(",")
    writing = "Edit" in tools or "Write" in tools
    role = "coder" if writing else (schema_kind(system) or "text")
    action = next_action(role)
    log(role=role, argv=ARGS, cwd=str(Path.cwd()), system=system, prompt=prompt,
        budget=value_after("--max-budget-usd"), session=value_after("--resume") or value_after("--session-id"))
    if action is None:
        fail({"stderr": f"claude stub: no scripted action for role {role}"})
    if "fail" in action:
        fail(action)
    if action.get("limit"):
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "You've hit your 5-hour limit · resets 6:30pm",
                          "session_id": "s-limit", "total_cost_usd": 0}))
        return
    apply_writes(action, Path.cwd())
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": reply_text(action), "session_id": value_after("--resume") or value_after("--session-id") or "s-1",
                      "total_cost_usd": action.get("cost", 0.1), "duration_ms": 1200,
                      "usage": {"input_tokens": (action.get("tokens") or [100, 20])[0],
                                "output_tokens": (action.get("tokens") or [100, 20])[1]}}))


# ------------------------------------------------------------------ codex

def codex() -> None:
    prompt = ARGS[-1]
    out_file = value_after("-o")
    sandbox = value_after("-s") or re.sub(r'.*sandbox_mode="([^"]+)".*', r"\1", " ".join(ARGS))
    writing = sandbox == "workspace-write"
    role = "coder" if writing else (schema_kind(prompt) or "text")
    action = next_action(role)
    log(role=role, argv=ARGS[:-1], cwd=str(Path.cwd()), sandbox=sandbox, prompt=prompt)
    if action is None:
        fail({"stderr": f"codex stub: no scripted action for role {role}"})
    if "fail" in action:
        fail(action)
    text = reply_text(action)
    if action.get("sandbox_denied"):
        text = action.get("reply") or "All failures were sandbox-denied socket binds; I changed nothing."
    else:
        apply_writes(action, Path.cwd())
    events = [{"type": "thread.started", "thread_id": "t-1"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed", "usage": {"input_tokens": (action.get("tokens") or [1000, 50])[0],
                                                  "output_tokens": (action.get("tokens") or [1000, 50])[1]}}]
    if out_file:
        Path(out_file).write_text(text)
    print("\n".join(json.dumps(e) for e in events))


# ------------------------------------------------------------------ openclaw

def openclaw() -> None:
    message = Path(value_after("--message-file")).read_text()
    role = schema_kind(message) or "text"
    action = next_action(role)
    log(role=role, argv=ARGS, message=message, session=value_after("--session-key"))
    if action is None:
        fail({"stderr": f"openclaw stub: no scripted action for role {role}"})
    if action.get("outage"):
        sys.stderr.write("[openclaw] The CLI command failed.\n[openclaw] For help, run `openclaw doctor`.\n")
        sys.exit(1)
    if "fail" in action:
        fail(action)
    usage = action.get("tokens") or [400, 80]
    print(json.dumps({"status": "ok", "runId": "r-1", "summary": "completed", "result": {
        "payloads": [{"text": reply_text(action)}],
        "meta": {"durationMs": 900, "agentMeta": {"model": "stub", "sessionId": "p-1",
                                                    "usage": {"input": usage[0], "output": usage[1]}}}}}))


# ------------------------------------------------------------------ gh

def gh_state() -> dict:
    return load(GH, {})


def save(state: dict) -> None:
    GH.write_text(json.dumps(state))


def gh() -> None:
    state, a = gh_state(), ARGS
    log(argv=a, cwd=str(Path.cwd()))
    if a[:2] == ["auth", "status"]:
        sys.exit(0 if state.get("auth_ok", True) else 1)
    if a[:2] == ["api", "user"]:
        print(state.get("user", "devflow-bot"))
        return
    if a[:2] == ["api", "rate_limit"]:
        print(json.dumps({"resources": state.get("rate_limit", {
            "core": {"remaining": 4900, "reset": time.time() + 3000},
            "graphql": {"remaining": 4900, "reset": time.time() + 3000}})}))
        return
    if a[:2] == ["issue", "view"]:
        issue = state["issues"].get(a[2])
        if issue is None:
            sys.stderr.write("issue not found\n")
            sys.exit(1)
        if state.get("rate_limited"):
            sys.stderr.write("GraphQL: API rate limit exceeded for user ID 1\n")
            sys.exit(1)
        print(json.dumps({"number": int(a[2]), **issue, "labels": [{"name": x} for x in issue.get("labels", [])],
                          "author": {"login": issue.get("author", "alice")}}))
        return
    if a[0] == "api" and "/issues/comments/" in a[1] and "PATCH" in a:
        cid = int(a[1].rsplit("/", 1)[1])
        body = json.loads(sys.stdin.read())["body"]
        for items in state["comments"].values():
            for c in items:
                if c["id"] == cid:
                    c["body"] = body
        save(state)
        return
    m = re.match(r"repos/[^/]+/[^/]+/issues/(\d+)/comments", a[1] if a[0] == "api" and len(a) > 1 else "")
    if m:
        number = m.group(1)
        if "POST" in a:
            body = json.loads(sys.stdin.read())["body"]
            state["next_id"] = state.get("next_id", 1000) + 1
            state["comments"].setdefault(number, []).append(
                {"id": state["next_id"], "user": {"login": state.get("user", "devflow-bot")}, "body": body,
                 "created_at": "2026-11-06T12:00:00+00:00"})
            save(state)
            print(state["next_id"])
        else:
            print(json.dumps(state["comments"].get(number, [])))
        return
    if a[:2] == ["pr", "list"]:
        head = value_after("--head")
        print(json.dumps([{"number": n, "url": p["url"], "headRefName": p["head"], "baseRefName": p["base"]}
                          for n, p in state["prs"].items() if p["head"] == head and p["state"] == "OPEN"]))
        return
    if a[:2] == ["pr", "create"]:
        number = str(state.get("next_pr", 100))
        state["next_pr"] = int(number) + 1
        state["prs"][number] = {"url": f"https://github.com/acme/widgets/pull/{number}", "head": value_after("--head"),
                                "base": value_after("--base"), "title": value_after("--title"), "state": "OPEN",
                                "merged": False, "body": sys.stdin.read()}
        save(state)
        print(state["prs"][number]["url"])
        return
    if a[:2] == ["pr", "view"]:
        pr = state["prs"][a[2]]
        if "statusCheckRollup" in a[-1]:
            checks = state.get("checks", "success")
            print(json.dumps({"statusCheckRollup": [] if checks == "none" else [{"conclusion": checks.upper()}]}))
        else:
            print(json.dumps({"number": int(a[2]), "url": pr["url"], "headRefName": pr["head"],
                              "baseRefName": pr["base"], "state": pr["state"],
                              "mergedAt": "2026-11-06T12:30:00Z" if pr["merged"] else None}))
        return
    if a[:2] == ["pr", "merge"]:
        if state.get("merge_error"):
            sys.stderr.write(state["merge_error"] + "\n")
            sys.exit(1)
        pr = state["prs"][a[2]]
        pr["merged"], pr["state"] = True, "MERGED"
        state.setdefault("merges", []).append({"pr": a[2], "match_head": value_after("--match-head-commit")})
        save(state)
        return
    if a[:2] == ["label", "create"]:
        return
    if a[:2] == ["issue", "list"]:
        print("[]")
        return
    sys.stderr.write(f"gh stub: unsupported command {a}\n")
    sys.exit(2)


{"claude": claude, "codex": codex, "openclaw": openclaw, "gh": gh}[TOOL]()
