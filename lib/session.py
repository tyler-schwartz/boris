import json, os, shutil, sqlite3, subprocess, sys
from cursor_models import resolve_model

def emit(agent, model):
    if not agent or not model:
        sys.exit("chat session has no agent or model")
    print(agent)
    print(model)
    raise SystemExit(0)

def agent_name(instance_id):
    key = instance_id.lower()
    if key == "claudeagent" or key.startswith("claude"):
        return "claude"
    if key.startswith("cursor"):
        return "cursor"
    if key.startswith("codex"):
        return "codex"
    return key

def from_t3():
    thread_id = os.environ.get("T3CODE_THREAD_ID")
    project = os.environ.get("T3CODE_PROJECT_ROOT")
    if not thread_id and not project:
        return None
    if shutil.which("t3cli") is None:
        if thread_id:
            sys.exit("t3 thread is set but t3cli is not installed")
        return None
    shown = subprocess.run(
        ["t3cli", "thread", "show", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if shown.returncode != 0:
        if thread_id:
            sys.exit(shown.stderr.strip() or "t3cli thread show failed")
        return None
    data = json.loads(shown.stdout)
    selection = data.get("modelSelection") or {}
    instance_id = selection.get("instanceId") or ""
    model = selection.get("model") or ""
    if not instance_id or not model:
        sys.exit("t3 thread has no modelSelection")
    emit(agent_name(instance_id), model)

def from_claude():
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    projects = os.path.join(config, "projects")
    path = None
    filename = f"{session_id}.jsonl"
    for dirpath, _dirnames, filenames in os.walk(projects):
        if filename in filenames:
            path = os.path.join(dirpath, filename)
            break
    if path is None:
        sys.exit(f"no Claude session file for {session_id}")
    model = None
    with open(path) as handle:
        for line in handle:
            if '"model"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message")
            if isinstance(message, dict) and message.get("model"):
                model = message["model"]
    if not model:
        sys.exit(f"Claude session {session_id} has no model")
    emit("claude", model)

def cursor_db():
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    candidates = [
        os.path.join(
            home,
            "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        ),
        os.path.join(xdg, "Cursor/User/globalStorage/state.vscdb"),
        os.path.join(
            home, ".cursor-server/data/User/globalStorage/state.vscdb"
        ),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            os.path.join(appdata, "Cursor/User/globalStorage/state.vscdb")
        )
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

def cursor_cli_model(model_id, parameters):
    return resolve_model(model_id, parameters)


def composer_model(db, conversation_id):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{conversation_id}",),
    ).fetchone()
    con.close()
    if row is None:
        return None
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    data = json.loads(raw)
    config = data.get("modelConfig") or {}
    selected = config.get("selectedModels") or []
    model_id = ""
    parameters = []
    if selected:
        model_id = selected[0].get("modelId") or ""
        parameters = selected[0].get("parameters") or []
    if not model_id:
        model_id = config.get("modelName") or ""
    if not model_id:
        sys.exit("Cursor chat has no model")
    return cursor_cli_model(model_id, parameters)

def cli_config_model():
    path = os.path.join(os.path.expanduser("~"), ".cursor", "cli-config.json")
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        data = json.load(handle)
    selected = data.get("selectedModel") or {}
    model_id = selected.get("modelId") or ""
    parameters = selected.get("parameters") or []
    if not model_id:
        model_id = (data.get("model") or {}).get("modelId") or ""
    if not model_id:
        return None
    return cursor_cli_model(model_id, parameters)

def from_cursor():
    conversation_id = os.environ.get("CURSOR_CONVERSATION_ID")
    if not conversation_id:
        return None
    db = cursor_db()
    model_id = composer_model(db, conversation_id) if db else None
    if model_id:
        emit("cursor", model_id)
    # Remote SSH keeps composer state on the Cursor UI machine.
    model_id = cli_config_model()
    if model_id:
        print(
            "boris: chat state database is not on this machine; "
            "using ~/.cursor/cli-config.json",
            file=sys.stderr,
        )
        emit("cursor", model_id)
    if db is None:
        sys.exit(
            "Cursor chat has no state database. On a remote host, "
            "set the model in ~/.cursor/cli-config.json."
        )
    sys.exit(f"no composer data for {conversation_id}")

if __name__ == '__main__':
    try:
        from_t3()
        from_claude()
        from_cursor()
        sys.exit("this chat did not identify an agent")
    except (ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
