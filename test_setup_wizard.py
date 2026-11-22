"""Exercise the public setup entry point with a profile and offline builders."""

import argparse
from contextlib import closing
import hashlib
import importlib
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest


OWNER = "628120001111"
BOT = "628120002222"
OTHER = "628120003333"
SECRET_NAME = "WA_CONTROL_LIVE_TEST_KEY"
SECRET = "fixture-key-that-must-stay-private"


@pytest.fixture
def wizard(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("_fixture_setup_wizard", Path(__file__).with_name("__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    module = importlib.import_module(f"{spec.name}.setup_wizard")
    launcher = plugin._launcher()
    home = tmp_path / "profiles" / "cobalt"
    home.mkdir(parents=True)
    data = home / "plugin-data" / "wa-control-live"
    state = data / "unified"
    settings, secrets = {}, {}
    installs, links, messages = [], [], []
    scanned = [BOT]
    ctx = SimpleNamespace(
        state=SimpleNamespace(data_dir=data),
        get_config=lambda key, default=None: settings.get(key, default),
        set_config=settings.__setitem__,
    )

    binary = tmp_path / "bundled-sidecar"
    binary.write_bytes(b"test arm64 sidecar")
    binary.chmod(0o700)
    bundled = {
        "binary": binary, "python": Path(sys.executable), "hermes_runtime": tmp_path,
        "sidecar_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }

    def install(_launcher, private, sidecar, binary, python, expected, *, require_codex=True):
        assert private == state
        assert require_codex is False
        assert len(expected) == 1
        installs.append(state)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
        shutil.copyfile(sidecar, binary)
        python.chmod(0o700)
        binary.chmod(0o700)

    def pair(config):
        links.append(config["bot_number"])
        private = Path(config["private"])
        store = private / "whatsapp" / "whatsapp.db"
        store.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(store)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS device (pn TEXT)")
            db.execute("DELETE FROM device")
            db.execute("INSERT INTO device VALUES (?)", (f"{scanned[0]}@s.whatsapp.net",))
        launcher.verify_paired_identity(config)
        (private / "paired.json").touch()

    def no_stock_identity(*_args):
        raise module.StockBotNotPaired()

    monkeypatch.setattr(module, "resolve_setup_identity", no_stock_identity)
    monkeypatch.setattr(plugin, "_bundled_runtime", lambda: bundled)
    monkeypatch.setattr(module, "prepare_control", lambda _path: None)
    monkeypatch.setattr(module, "_profile_env", lambda _owner, pending: (pending, secrets.__setitem__))
    monkeypatch.setattr(plugin, "_setup", install)
    monkeypatch.setattr(launcher, "pair", pair)
    return SimpleNamespace(
        module=module, plugin=plugin, launcher=launcher, ctx=ctx, home=home, state=state, settings=settings,
        secrets=secrets, installs=installs, links=links, messages=messages, scanned=scanned,
    )


def _setup(wizard, answers):
    answers = iter(answers)
    return wizard.module.run_setup(
        wizard.ctx, wizard.home, wizard.launcher, ask=lambda _prompt: next(answers), tell=wizard.messages.append,
    )


def test_bare_setup_dispatches_to_wizard(wizard, monkeypatch):
    parser = argparse.ArgumentParser()
    wizard.plugin._parser(parser)
    captured = []
    monkeypatch.setattr(wizard.module, "run_setup", lambda *args: captured.append(args))
    monkeypatch.setattr(wizard.plugin, "_launcher", lambda: wizard.launcher)
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: wizard.home))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    wizard.plugin._run(wizard.ctx, parser.parse_args(["setup"]))

    assert captured == [(wizard.ctx, wizard.home, wizard.launcher)]
    manual = parser.parse_args(["setup", "--sidecar", "/tmp/sidecar", "--sidecar-sha256", "a" * 64])
    assert (manual.sidecar, manual.sidecar_sha256) == (Path("/tmp/sidecar"), "a" * 64)


def test_fresh_setup_uses_bundled_runtime_with_optional_live_deferred(wizard, monkeypatch):
    monkeypatch.setattr(wizard.module, "choose_live", lambda *_args, **_kwargs: (None, {}))

    _setup(wizard, [f"+{BOT}", f"+{OWNER}", "personal", ""])

    assert wizard.installs == []
    assert wizard.links == [BOT]
    assert (wizard.state / "paired.json").is_file()
    assert wizard.settings["unified_gateway"] is True
    assert wizard.settings["owner_number"] == OWNER
    assert wizard.settings["bot_number"] == BOT
    assert wizard.settings["live_enabled"] is False
    assert any("whatsapp-control pair personal" in line for line in wizard.messages)
    assert any("Scan the Live QR" in line for line in wizard.messages)
    assert any("runs with this profile's Hermes gateway" in line for line in wizard.messages)
    live_list = wizard.plugin._live_operation(wizard.ctx, wizard.state, wizard.launcher, "list")
    assert live_list["providers"] == []
    assert live_list["live_enabled"] is False

    _setup(wizard, ["", "", ""])

    assert wizard.installs == []
    assert wizard.links == [BOT]

    with pytest.raises(ValueError, match="already linked to another bot"):
        _setup(wizard, [OTHER])
    assert wizard.settings["bot_number"] == BOT
    assert wizard.links == [BOT]


def test_wrong_pairing_keeps_setup_unpublished_then_retry_saves_live_secret(wizard, monkeypatch):
    live = {
        "selected": "gemini",
        "providers": {"gemini": {"kind": "gemini", "accounts": {"default": {"secret_env": SECRET_NAME}}}},
    }
    monkeypatch.setattr(wizard.module, "choose_live", lambda *_args, **_kwargs: (live, {SECRET_NAME: SECRET}))
    wizard.scanned[0] = OTHER
    with pytest.raises(RuntimeError, match="mismatched"):
        _setup(wizard, [BOT, OWNER, "", ""])

    assert not wizard.settings.get("unified_gateway")
    assert "live" not in wizard.settings
    assert SECRET_NAME not in wizard.secrets
    assert not (wizard.state / "paired.json").exists()
    assert any("To retry bot pairing, run: hermes -p cobalt wa-control-live setup" in line for line in wizard.messages)

    wizard.scanned[0] = BOT
    _setup(wizard, [BOT, OWNER, "", ""])

    assert wizard.settings["live"] == live
    assert wizard.settings["live_enabled"] is True
    assert wizard.secrets[SECRET_NAME] == SECRET
    assert SECRET not in repr(wizard.settings)
    assert SECRET not in "\n".join(wizard.messages)


def test_stock_setup_uses_detected_bot_and_confirms_owner_with_prebuilt_sidecar(wizard, monkeypatch, tmp_path):
    seen = []

    def detect(home, source):
        seen.append((home, source))
        return BOT, OWNER

    monkeypatch.setattr(wizard.module, "resolve_setup_identity", detect)
    monkeypatch.setattr(wizard.module, "choose_live", lambda *_args, **_kwargs: (None, {}))
    sidecar = tmp_path / "prepared-sidecar"
    sidecar.write_bytes(b"test arm64 sidecar")
    answers = iter(["y", "personal", "n"])
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    wizard.module.run_setup(
        wizard.ctx, wizard.home, wizard.launcher, stock_profile="default", sidecar=sidecar,
        sidecar_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        ask=ask, tell=wizard.messages.append,
    )
    assert seen == [(wizard.home, "default")]
    assert wizard.settings["bot_number"] == BOT
    assert wizard.settings["owner_number"] == OWNER
    assert wizard.links == []
    assert wizard.installs == [wizard.state]
    assert "as Live caller and DM owner?" in prompts[0]
    assert not any("number (with country code)" in prompt for prompt in prompts)


def test_stock_setup_requires_owner_approval_separately_from_pairing(wizard, monkeypatch):
    monkeypatch.setattr(wizard.module, "resolve_setup_identity", lambda *_: (BOT, OWNER))
    monkeypatch.setattr(wizard.module, "choose_live", lambda *_args, **_kwargs: (None, {}))
    _setup(wizard, ["n", OTHER, "", "n"])
    assert wizard.settings["bot_number"] == BOT
    assert wizard.settings["owner_number"] == OTHER
    assert wizard.links == []


def test_first_pair_uses_profile_source_and_package_without_separate_setup(wizard, monkeypatch, capsys):
    seen = []

    def detect(home, source):
        seen.append((home, source))
        return BOT, OWNER

    monkeypatch.setattr(wizard.module, "resolve_setup_identity", detect)
    monkeypatch.setattr(wizard.module.shutil, "which", lambda name: "/fixture/codex" if name == "codex" else None)
    wizard.settings["stock_profile"] = "default"
    parser = argparse.ArgumentParser()
    wizard.plugin._parser(parser)
    monkeypatch.setattr(wizard.plugin, "_launcher", lambda: wizard.launcher)
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: wizard.home))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run_setup = wizard.module.run_setup
    answers = iter(["y"])
    monkeypatch.setattr(wizard.module, "run_setup", lambda *args, **options:
                        run_setup(*args, ask=lambda _prompt: next(answers), **options))

    wizard.plugin._run(wizard.ctx, parser.parse_args(["pair"]))

    assert seen == [(wizard.home, "default")]
    assert wizard.installs == []
    assert wizard.links == [BOT]
    assert not (wizard.state / "venv").exists()
    assert wizard.settings["live"]["selected"] == "codex"
    assert "hermes -p cobalt whatsapp-control pair personal" in capsys.readouterr().out


@pytest.mark.parametrize("reuse", [False, True])
def test_provider_menu_reuses_profile_secret_or_stages_only_new_reference(wizard, monkeypatch, reuse):
    existing = {"GEMINI_API_KEY": "profile-key", "WA_CONTROL_LIVE_GEMINI_KEY": "occupied"}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", SimpleNamespace(get_env_value=existing.get))
    answers = iter(["1", "2", "", "gemini-live-fixture", "Aoede"] if reuse else
                   ["2", "3", "", "gemini-live-fixture", "Aoede", ""])
    masked = []

    def read_secret(prompt):
        masked.append(prompt)
        return SECRET

    settings, secrets = wizard.module.choose_live(
        wizard.ctx, ask=lambda _prompt: next(answers), tell=wizard.messages.append, read_secret=read_secret,
    )

    assert settings["selected"] == "gemini"
    gemini = settings["providers"]["gemini"]
    assert (gemini["kind"], gemini["model"], gemini["voice"]) == ("gemini", "gemini-live-fixture", "Aoede")
    reference = "GEMINI_API_KEY" if reuse else "WA_CONTROL_LIVE_GEMINI_2_KEY"
    assert gemini["accounts"]["default"]["secret_env"] == reference
    assert secrets == ({} if reuse else {reference: SECRET})
    assert len(masked) == int(not reuse)
    assert SECRET not in repr(settings)
    assert SECRET not in "\n".join(wizard.messages)
