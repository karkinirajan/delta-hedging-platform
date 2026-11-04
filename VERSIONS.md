# Versions

Exact versions of every locked package are in `uv.lock`; these are the ones that matter.

| Component | Version | Pinned in |
|---|---|---|
| Python | 3.13 (3.13.18 used for the tests) | `.python-version`, `requires-python = ">=3.13"` |
| Flask | 3.1.3 (`>=3.1`) | `pyproject.toml`, `uv.lock` |
| Werkzeug / Jinja2 | 3.1.9 / 3.1.6 | `uv.lock` (via Flask) |
| httpx | 0.28.1 (`>=0.28`) | `pyproject.toml`, `uv.lock` |
| python-dotenv / gunicorn | 1.2.4 / 26.2.0 | `uv.lock` |
| pytest / pytest-cov / ruff | 9.2.0 / 7.1.0 / 0.17.3 | dev group |
| uv | 0.13.7 locally, 0.13.6 in the Dockerfile | used to build the lock |
| IG REST API | the demo gateway, per-endpoint versions (see below) | `IG_BASE_URL`, `ig.py` |

25 third-party packages are locked (26 entries with the project itself).

## Where the IG facts come from

The endpoint versions and behaviours the client relies on were read from IG's documentation at
[labs.ig.com](https://labs.ig.com/) in a research pass before the client was written, and are recorded in the module
docstring of `src/delta_hedging/ig.py`:

* `POST /session` v2 (tokens in the `CST` and `X-SECURITY-TOKEN` response headers); `PUT /session` to switch account;
  `GET /positions` v2; `GET /markets/{epic}` v3; `POST /positions/otc` v2, which answers with a `dealReference` and
  nothing else; `GET /confirms/{dealReference}` v1.
* An exceeded allowance is HTTP 403 with an `error.public-api.exceeded-*` code.
* The patterns for a deal reference, an account id and an epic.

**None of this was checked against the live service.** No request was ever sent to IG; the tests and the end-to-end
run use a fake written from the same documentation. IG's documentation lists no strike or underlying field on a
position, so the strike and the option type are read from the instrument's name.

## What the first version ran on

31 pinned lines, among them Flask 3.1.0, Flask-Cors 5.0.0, NumPy 2.2.1, SciPy 1.15.1, requests 2.32.3 and, from an
interactive session that was frozen into the file, IPython and its helpers. The page loaded Tailwind from
`cdn.tailwindcss.com`. Python "3.8+" was claimed in the README.
