"""Generate local credentials and enable tracing for explicit repository paths."""
import argparse
import json
import secrets
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--capture-content", action="store_true", help="Capture redacted prompts, responses and tool payloads locally")
parser.add_argument("repos", nargs="*")
parser.add_argument("--data-dir", type=Path, help="Directory for local generated credentials")
args = parser.parse_args()

root = (args.data_dir or Path(__file__).resolve().parent).resolve()
root.mkdir(parents=True, exist_ok=True)
env_file = root / ".env"
if env_file.exists():
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
else:
    values = {key: secrets.token_hex(32) for key in (
        "POSTGRES_PASSWORD", "SALT", "ENCRYPTION_KEY", "CLICKHOUSE_PASSWORD",
        "REDIS_AUTH", "MINIO_PASSWORD", "NEXTAUTH_SECRET", "ADMIN_PASSWORD")}
    values["LANGFUSE_PUBLIC_KEY"] = "pk-lf-" + secrets.token_hex(16)
    values["LANGFUSE_SECRET_KEY"] = "sk-lf-" + secrets.token_hex(16)
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    env_file.chmod(0o600)
for arg in args.repos:
    repo = Path(arg).resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"Not a git checkout: {repo}")
    state = repo / ".devflow/state"
    state.mkdir(parents=True, exist_ok=True)
    config = {"enabled": True, "endpoint": "http://127.0.0.1:3300/api/public/otel/v1/traces",
              "public_key": values["LANGFUSE_PUBLIC_KEY"], "secret_key": values["LANGFUSE_SECRET_KEY"],
              "log_file": str(state / "traces.jsonl")}
    target = state / "telemetry.json"
    if target.exists():
        previous = json.loads(target.read_text())
        config["capture_content"] = previous.get("capture_content") is True
        config["max_content_characters"] = previous.get("max_content_characters", 200_000)
    if args.capture_content:
        config["capture_content"] = True
    target.write_text(json.dumps(config, indent=2))
    target.chmod(0o600)
login = root / "login.json"
login.write_text(json.dumps({"url": "http://localhost:3300", "email": "admin@devflow.local",
                            "password": values["ADMIN_PASSWORD"]}, indent=2))
login.chmod(0o600)
print(f"Local credentials saved in {login}; secret values were not printed.")
