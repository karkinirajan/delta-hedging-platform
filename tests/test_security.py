import pathlib

from .conftest import AUTH


def test_security_headers_are_on_every_response(app):
    client = app.test_client()
    for path in ("/", "/healthz", "/api/analysis", "/nothing", "/static/dashboard.js", "/static/style.css"):
        with client.get(path, headers=AUTH) as response:
            h = response.headers
            assert (
                "default-src 'self'" in h["Content-Security-Policy"]
                and "frame-ancestors 'none'" in h["Content-Security-Policy"]
            ), path
            assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY", path
            assert h["Referrer-Policy"] == "no-referrer" and h["Cross-Origin-Resource-Policy"] == "same-origin", path


def test_api_answers_are_never_cached(app):
    client = app.test_client()
    for path in ("/api/analysis", "/api/hedge/history", "/"):
        assert client.get(path, headers=AUTH).headers["Cache-Control"] == "no-store"


def test_the_policy_allows_no_inline_script_or_style_and_no_other_site(app):
    policy = app.test_client().get("/").headers["Content-Security-Policy"]
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy and "http" not in policy


def test_there_is_no_cors_so_other_sites_cannot_call_the_api(app):
    """The first version had CORS(app): any web page the operator opened could have placed an order."""
    client = app.test_client()
    origin = {"Origin": "https://evil.example.test"}
    preflight = client.options(
        "/api/hedge",
        headers={**origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"},
    )
    assert "Access-Control-Allow-Origin" not in preflight.headers
    reply = client.get("/api/analysis", headers={**AUTH, **origin})
    assert not any(name.lower().startswith("access-control-") for name, _ in reply.headers)


def test_the_dashboard_has_no_inline_code_and_loads_nothing_from_other_sites(app):
    for template in pathlib.Path(app.root_path, "templates").glob("*.html"):
        text = template.read_text()
        assert " style=" not in text and "<style" not in text and " onclick=" not in text, template.name
        assert "http://" not in text and "https://" not in text, template.name
        for tag in text.split("<script")[1:]:
            assert "src=" in tag.split(">")[0], template.name


def test_the_static_files_load_nothing_from_other_sites_and_never_use_innerhtml(app):
    for f in pathlib.Path(app.root_path, "static").glob("*"):
        if f.suffix in (".css", ".js"):
            text = f.read_text()
            assert "http://" not in text and "https://" not in text and "@import" not in text, f.name
            for forbidden in (".innerHTML", "insertAdjacentHTML", "eval(", "document.write", "new Function"):
                assert forbidden not in text, (f.name, forbidden)


def test_debug_mode_is_off_and_there_is_no_console(app):
    assert app.debug is False and app.test_client().get("/console").status_code == 404
