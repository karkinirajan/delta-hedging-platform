"""Things that went wrong in the first version and must not come back."""

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "delta_hedging"


def git_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [p for p in out.split("\0") if p]


def tracked_text_files():
    suffixes = (".py", ".html", ".md", ".toml", ".yml", ".example", ".css", ".js")
    files = [ROOT / p for p in git_files() if p.endswith(suffixes) and p != "uv.lock"]
    return [f for f in files if f.exists()] or [f for f in ROOT.rglob("*.py") if ".venv" not in f.parts]


def test_no_credentials_or_account_ids_in_the_tree():
    shapes = {
        "an OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
        "an AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "a Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    }
    named = re.compile(
        r"""(?i)(api_?key|secret|token|passw\w*|username|account_?id)\w*\s*[=:]\s*['"][A-Za-z0-9_/@+.-]{6,}['"]"""
    )
    found = []
    for f in tracked_text_files():
        if f.name == "test_hygiene.py":
            continue
        text = f.read_text(errors="ignore")
        relative = f.relative_to(ROOT)
        found += [f"{relative}: {label}" for label, p in shapes.items() if p.search(text)]
        if relative.parts[0] != "tests" and named.search(text):
            found.append(f"{relative}: a literal assigned to a key, password, username or account name")
    assert found == []


def test_no_database_log_or_env_file_is_tracked():
    bad = [
        p
        for p in git_files()
        if p.endswith((".db", ".sqlite3", ".log")) or p.split("/")[-1] == ".env" or p.startswith("logs/")
    ]
    assert bad == []


def test_no_ip_address_is_written_in_the_code():
    """The first version's web page had the public address of a server as its API URL (over plain HTTP)."""
    pattern = re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b")
    hits = []
    for f in list(SRC.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".js", ".html", ".css"):
            hits += [f.name for _ in pattern.finditer(f.read_text())]
    assert hits == []


def test_a_server_is_started_only_by_the_cli_and_never_in_debug_mode():
    for f in SRC.rglob("*.py"):
        calls = [n for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.Call)]
        for call in calls:
            for keyword in call.keywords:
                assert not (keyword.arg == "debug" and getattr(keyword.value, "value", None) is True), f.name
        runs = [c for c in calls if getattr(c.func, "attr", "") == "run"]
        assert not runs or f.name == "cli.py", f.name


def test_no_print_calls_no_cors_and_no_blocking_requests_library():
    for f in SRC.rglob("*.py"):
        tree = ast.parse(f.read_text())
        calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "print" not in calls or f.name == "cli.py", f.name  # the first version printed account ids
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not imported & {"requests", "flask_cors", "numpy", "scipy"}, f.name


def test_only_the_demo_url_is_in_the_code_and_the_live_one_is_not():
    text = "".join(f.read_text() for f in SRC.rglob("*.py"))
    assert "https://demo-api.ig.com/gateway/deal" in text
    assert "https://api.ig.com" not in text


def test_no_instrument_or_account_is_hard_coded_in_the_code():
    """The first version fixed the underlying to one index (and ignored the epic it was given)."""
    for f in SRC.rglob("*.py"):
        assert "IX.D." not in f.read_text(), f.name


def test_every_http_call_has_a_timeout():
    text = (SRC / "ig.py").read_text()
    assert "httpx.Client(timeout=settings.request_timeout)" in text


def test_the_ig_session_headers_are_never_logged():
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("debug", "info", "warning", "error", "exception")
            ):
                source = ast.unparse(node)
                for forbidden in (
                    "headers",
                    "password",
                    "_token",
                    "_cst",
                    "ig_api_key",
                    "response.text",
                    "response.content",
                ):
                    assert forbidden not in source, (f.name, source[:80])


def test_env_example_lists_every_variable_the_settings_read():
    source = (SRC / "config.py").read_text()
    read = set(re.findall(r"""(?:get|_number)\(\s*(?:environ,\s*)?["']([A-Z][A-Z0-9_]+)["']""", source))
    read |= set(re.findall(r'"([A-Z][A-Z0-9_]+)"', re.search(r"required = \[.*?\]", source, re.S).group(0)))
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert {"IG_API_KEY", "IG_USERNAME", "IG_PASSWORD", "API_TOKEN", "HEDGING_ENABLED", "IG_UNDERLYING_EPIC"} <= read
    assert sorted(read - documented) == []


def test_env_example_ships_with_hedging_off():
    text = (ROOT / ".env.example").read_text()
    assert re.search(r"^HEDGING_ENABLED=(false|)\s*$", text, re.M)
