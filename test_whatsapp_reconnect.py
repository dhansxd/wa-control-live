"""Exercise the platform registration inside stock Hermes lifecycle paths."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

run = pytest.importorskip("gateway.run")
from agent.secret_scope import is_multiplex_active, set_multiplex_active
from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import BasePlatformAdapter
from hermes_cli.plugins import get_plugin_manager
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from whatsapp_reconnect import register_whatsapp_platform


PLUGIN = "whatsapp-reconnect-fixture"


class LocalAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform.WHATSAPP)
        self.connects = []

    async def connect(self, *, is_reconnect=False):
        self.connects.append(is_reconnect)
        return True

    async def disconnect(self):
        pass

    async def send(self, *args, **kwargs):
        raise AssertionError("Unexpected outbound operation during reconnect test")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


def configure_fixture(home, *, explicit=True):
    installed = home / "plugins" / PLUGIN
    installed.mkdir(parents=True)
    token = home / "rust" / "adapter.token"
    token.parent.mkdir()
    token.touch()
    (installed / "plugin.yaml").write_text(f"name: {PLUGIN}\nversion: 0.0.1\n")
    (installed / "__init__.py").write_text(
        "from pathlib import Path\n"
        "from test_whatsapp_reconnect import LocalAdapter, register_whatsapp_platform\n"
        "def register(ctx):\n"
        f"    register_whatsapp_platform(ctx, LocalAdapter, Path({str(token)!r}))\n"
    )
    platform = "platforms:\n  whatsapp:\n    enabled: true\n" if explicit else ""
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{PLUGIN}]\n" + platform)
    return token.with_name("paired.json")


@pytest.mark.parametrize("failure", ["platform declined", "unload callback failed"])
def test_rejected_registration_cleans_up_private_reference(tmp_path, failure):
    seen = []

    def platform(**kwargs):
        directory = Path(kwargs["env_enablement_fn"]()["session_path"])
        assert (directory / "creds.json").readlink() == tmp_path / "paired.json"
        seen.append(directory)
        return None if failure == "platform declined" else object()

    def on_unload(_callback):
        raise RuntimeError(failure)

    ctx = SimpleNamespace(register_platform=platform, on_unload=on_unload)
    with pytest.raises(RuntimeError, match="could not register|callback failed"):
        register_whatsapp_platform(ctx, LocalAdapter, tmp_path / "adapter.token")
    directory, = seen
    assert not directory.exists()


def test_explicitly_disabled_whatsapp_does_not_bind_active_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    proof = configure_fixture(tmp_path)
    proof.touch()
    (tmp_path / "config.yaml").write_text(
        f"plugins:\n  enabled: [{PLUGIN}]\nplatforms:\n  whatsapp:\n    enabled: false\n"
    )
    try:
        config = load_gateway_config().platforms[Platform.WHATSAPP]
        assert not config.enabled
        assert "session_path" not in config.extra
    finally:
        get_plugin_manager().unload()


@pytest.mark.parametrize("explicit", [True, False])
def test_stock_multiplex_prefilter_reconnect_and_unload(tmp_path, monkeypatch, explicit):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    proof = configure_fixture(home, explicit=explicit)
    platform = Platform.WHATSAPP
    native_probe = run._platform_has_bot_credential

    async def exercise():
        prior_multiplex = is_multiplex_active()
        config = load_gateway_config()
        config.multiplex_profiles = True
        platform_config = config.platforms[platform]
        reference = Path(platform_config.extra["session_path"])
        assert reference.stat().st_mode & 0o777 == 0o700
        assert (reference / "creds.json").readlink() == proof
        assert run._platform_has_bot_credential is native_probe
        runner = run.GatewayRunner(config)
        try:
            assert await runner._start_prefilter_platforms() == (False, 0, [platform], [])
            proof.touch()
            aborted, enabled, skipped, pending = await runner._start_prefilter_platforms()
            assert not aborted and enabled == 1 and not skipped
            _, _, predecessor = pending[0]
            retry = runner._startup_retry_entry(platform, predecessor, platform_config)
            retry["next_retry"] = 0
            runner._failed_platforms[platform] = retry
            await runner._reconnect_failed_platform(platform, time.monotonic())
            assert platform not in runner._failed_platforms
            assert runner.adapters[platform].connects == [True]

            get_plugin_manager().unload()
            assert not reference.exists()
            assert not native_probe(platform, platform_config)
            assert proof.is_file() and proof.with_name("adapter.token").is_file()
            native = home / "stock-wa"
            native.mkdir()
            (native / "creds.json").write_text("{}")
            (home / "config.yaml").write_text(
                f"plugins:\n  enabled: []\n  disabled: [{PLUGIN}]\n"
                "platforms:\n  whatsapp:\n    enabled: true\n"
                f"    extra:\n      session_path: {json.dumps(str(native))}\n"
            )
            stock_config = load_gateway_config().platforms[platform]
            assert Path(stock_config.extra["session_path"]) == native
            assert native_probe(platform, stock_config)
        finally:
            get_plugin_manager().unload()
            runner.close_all_session_db_handles()
            set_multiplex_active(prior_multiplex)

    asyncio.run(exercise())


def test_stock_secondary_gateway_uses_its_own_pairing_proof_on_boot_and_reconnect(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    profile_home = home / "profiles" / "secondary"
    monkeypatch.setenv("HERMES_HOME", str(home))
    proof = configure_fixture(profile_home)
    proof.touch()
    platform = Platform.WHATSAPP

    async def exercise():
        prior_multiplex = is_multiplex_active()
        runner = run.GatewayRunner(GatewayConfig(multiplex_profiles=True))
        try:
            assert await runner._start_one_profile_adapters("secondary", profile_home, {}) == 1
            predecessor = runner._profile_adapters["secondary"][platform]
            assert predecessor.connects == [False]
            adapter, connected = await runner._secondary_reconnect_attempt("secondary", platform, predecessor)
            assert connected and adapter.connects == [True]
            proof.unlink()
            assert await runner._secondary_reconnect_attempt("secondary", platform, adapter) == (None, None)
        finally:
            scope = set_hermes_home_override(profile_home)
            try:
                get_plugin_manager().unload()
            finally:
                reset_hermes_home_override(scope)
            runner.close_all_session_db_handles()
            set_multiplex_active(prior_multiplex)

    asyncio.run(exercise())
