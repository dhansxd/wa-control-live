"""Exercise the public cleanup command within a disposable Hermes profile."""

import argparse
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def installed(monkeypatch, tmp_path):
    import sys
    from hermes_cli import plugins_cmd

    spec = importlib.util.spec_from_file_location("_fixture_wa_purge", Path(__file__).with_name("__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    home = tmp_path / "profiles" / "test"
    data = home / "plugin-data" / "wa-control-live"
    data.mkdir(parents=True)
    ctx = SimpleNamespace(state=SimpleNamespace(data_dir=data, get=lambda _, default=None: default))
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: home))
    monkeypatch.setattr(plugins_cmd, "_find_plugin_entry", lambda _: None)
    parser = argparse.ArgumentParser()
    plugin._parser(parser)
    return plugin, ctx, home, data, parser


def _linked(private, number):
    store = private / "whatsapp" / "whatsapp.db"
    store.parent.mkdir(parents=True)
    with closing(sqlite3.connect(store)) as db, db:
        db.execute("CREATE TABLE device (pn TEXT)")
        db.execute("INSERT INTO device VALUES (?)", (number + "@s.whatsapp.net",))
    (private / "config.json").write_text(json.dumps({"bot_number": number}))
    (private / "paired.json").touch()


def test_public_cleanup_stops_and_purges_only_owned_profile_data(installed, capsys):
    plugin, ctx, home, data, parser = installed
    bot = "15550100002"
    _linked(data, bot)
    _linked(data / "unified", bot)
    control = home / "plugin-data" / "hermes-whatsapp-control"
    control.mkdir()
    (control / "accounts.json").write_text('{"personal": {}}')
    retained = [home / "state.db", home / "auth.json", home / "whatsapp" / "session" / "creds.json",
                home / "plugin-data" / "dyra-legacy" / "whatsapp.db",
                home.parent / "other" / "plugin-data" / "wa-control-live" / "adapter.token"]
    for marker in retained:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("retained")
    (data / "foreign-link").symlink_to(home.parent / "other", target_is_directory=True)

    plugin._run(ctx, parser.parse_args(["cleanup"]))
    assert json.loads(capsys.readouterr().out)["data_retained"] is True
    assert control.is_dir() and (data / "unified" / "paired.json").is_file()

    plugin._run(ctx, parser.parse_args(["cleanup", "--purge"]))
    report = json.loads(capsys.readouterr().out)
    assert report["stopped"] and report["local_data_removed"]
    assert report["legacy_runtime_removed"] and report["unified_runtime_removed"]
    assert {(row["account"], row.get("runtime")) for row in report["accounts"]} == {
        ("personal", None), ("bot", "legacy"), ("bot", "unified"),
    }
    assert all(row["local_removed"] and not row["remote_unlinked"] for row in report["accounts"])
    assert not control.exists()
    assert not (data / "foreign-link").is_symlink()
    assert not (data / "unified" / "whatsapp").exists()
    assert all(marker.read_text() == "retained" for marker in retained)

    plugin._run(ctx, parser.parse_args(["cleanup", "--purge"]))
    assert json.loads(capsys.readouterr().out)["accounts"] == []


def test_combined_cleanup_refuses_shared_standalone_control_storage(installed, monkeypatch):
    from hermes_cli import plugins_cmd

    plugin, ctx, home, _data, parser = installed
    other = home / "plugins" / "standalone-control"
    other.mkdir(parents=True)
    control = home / "plugin-data" / "hermes-whatsapp-control"
    control.mkdir()
    (control / "accounts.json").write_text('{"shared": {}}')
    monkeypatch.setattr(plugins_cmd, "_find_plugin_entry", lambda name: (name, "1", "", "user", other, name))

    with pytest.raises(RuntimeError, match="shares this profile"):
        plugin._run(ctx, parser.parse_args(["cleanup", "--purge"]))
    assert (control / "accounts.json").is_file()
