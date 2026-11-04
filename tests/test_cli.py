import pytest
from flask import Flask

from delta_hedging import cli

from .conftest import ENV


def apply_env(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_the_calculator_prints_the_textbook_values(capsys):
    argv = [
        "greeks",
        "--type",
        "call",
        "--spot",
        "100",
        "--strike",
        "100",
        "--years",
        "1",
        "--volatility",
        "0.2",
        "--rate",
        "0.05",
    ]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    values = {
        row.split(":")[0].strip(): float(row.split(":")[1])
        for row in out.splitlines()
        if row.strip()[:3] in ("pri", "del", "gam", "veg", "the", "rho")
    }
    assert values == pytest.approx(
        {"price": 10.4506, "delta": 0.6368, "gamma": 0.018762, "vega": 0.37524, "theta": -0.017570, "rho": 0.53232},
        abs=1e-4,
    )
    assert "per percentage point" in out


def test_the_calculator_takes_days_or_years(capsys):
    cli.main(
        [
            "greeks",
            "--type",
            "put",
            "--spot",
            "100",
            "--strike",
            "100",
            "--days",
            "365",
            "--volatility",
            "0.2",
            "--rate",
            "0.05",
        ]
    )
    days = capsys.readouterr().out
    cli.main(
        [
            "greeks",
            "--type",
            "put",
            "--spot",
            "100",
            "--strike",
            "100",
            "--years",
            "1",
            "--volatility",
            "0.2",
            "--rate",
            "0.05",
        ]
    )
    assert days.splitlines()[1:] == capsys.readouterr().out.splitlines()[1:]


@pytest.mark.parametrize(
    "args",
    [["--spot", "0"], ["--strike", "-1"], ["--volatility", "0"], ["--years", "0"]],
)
def test_the_calculator_refuses_invalid_inputs_with_a_message(args, capsys):
    base = {"--type": "call", "--spot": "100", "--strike": "100", "--years": "1", "--volatility": "0.2"}
    base.update(dict(zip(args[::2], args[1::2], strict=True)))
    argv = ["greeks", *[x for pair in base.items() for x in pair]]
    assert cli.main(argv) == 2
    assert "must be positive" in capsys.readouterr().err


def test_the_calculator_needs_no_ig_settings(monkeypatch, capsys):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    assert (
        cli.main(
            ["greeks", "--type", "call", "--spot", "100", "--strike", "100", "--days", "30", "--volatility", "0.2"]
        )
        == 0
    )


def test_a_missing_configuration_exits_2_naming_the_variables(capsys):
    assert cli.main(["run"]) == 2
    err = capsys.readouterr().err
    assert "IG_API_KEY" in err and "API_TOKEN" in err and ".env.example" in err


def test_run_starts_the_server_on_the_loopback_address_without_debug(monkeypatch, env):
    apply_env(monkeypatch, env)
    seen = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: seen.update(kwargs))
    assert cli.main(["run"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 8000, "debug": False}  # the first version: 0.0.0.0 and debug=True
    assert cli.main(["run", "--host", "0.0.0.0", "--port", "9000"]) == 0  # noqa: S104
    assert seen["port"] == 9000


def test_a_demo_only_setting_is_enforced_at_start(monkeypatch, env, capsys):
    apply_env(monkeypatch, {**env, "IG_ENVIRONMENT": "live"})
    assert cli.main(["run"]) == 2
    assert "only IG's demo environment" in capsys.readouterr().err


def test_a_dotenv_file_in_the_current_directory_is_read(monkeypatch, env, tmp_path):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: None)
    assert cli.main(["run"]) == 0
