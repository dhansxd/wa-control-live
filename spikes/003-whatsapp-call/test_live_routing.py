from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from live_routing import LiveProviderPool


def live_config(keys=2):
    return {
        "selected": "daily",
        "providers": {
            "subscription": {"kind": "codex"},
            "speech": {
                "kind": "gemini", "model": "models/gemini-live", "voice": "Aoede",
                "accounts": {f"g{i}": {"secret_env": f"LIVE_TEST_KEY_{i}"} for i in range(keys)},
            },
        },
        "pools": {"daily": ["subscription", "speech"]},
    }


def live_secrets(keys=2):
    return {f"LIVE_TEST_KEY_{i}": f"fixture-credential-{i}" for i in range(keys)}


def test_existing_config_selects_codex_without_new_settings():
    pool = LiveProviderPool(environ={})
    candidate = pool.next_candidate()
    assert (candidate.provider, candidate.kind, candidate.account) == ("codex", "codex", "default")
    assert pool.secret(candidate) is None
    assert pool.next_candidate(exclude={candidate.key}) is None
    assert pool.next_candidate().key == candidate.key


def test_hermes_realtime_uses_profile_sign_in_and_falls_back_in_pool():
    config = live_config(1)
    config["providers"]["profile"] = {
        "kind": "hermes-realtime", "model": "gpt-realtime", "voice": "marin",
    }
    config["pools"]["daily"] = ["profile", "speech"]
    now = [10.0]
    pool = LiveProviderPool(config, environ={**live_secrets(1), "OPENAI_API_KEY": "fixture"},
                            clock=lambda: now[0])
    realtime = pool.next_candidate()
    assert (realtime.key, realtime.kind) == (("profile", "default"), "hermes-realtime")
    assert (realtime.model, realtime.voice) == ("gpt-realtime", "marin")
    assert realtime.endpoint is None and pool.secret(realtime) is None
    assert pool.next_candidate().key == ("speech", "g0")
    assert pool.next_candidate().key == realtime.key

    pool.record_failure(realtime, "auth_required")
    status = pool.status("profile")
    assert status["accounts"][0]["reason"] == "auth_required"
    assert status["retry_estimated"] and 12 <= status["retry_at"] < 13
    assert pool.next_candidate().key == ("speech", "g0")
    assert pool.next_candidate("profile") is None

    now[0] = status["retry_at"]
    recovered = pool.next_candidate()
    assert recovered.key == realtime.key
    pool.record_ready(recovered)
    assert pool.status("profile")["accounts"][0]["reason"] is None


@pytest.mark.parametrize(("options", "reason"), [
    ({"accounts": {"primary": {}, "backup": {}}}, "Invalid Live accounts"),
    ({"accounts": {"primary": {"secret_env": "OPENAI_API_KEY"}}}, "Invalid Live account settings"),
    ({"endpoint": "wss://gateway.example.net/realtime"}, "requires kind gpt-live"),
])
def test_hermes_realtime_rejects_non_profile_credentials(options, reason):
    with pytest.raises(ValueError, match=reason):
        LiveProviderPool({"providers": {"profile": {"kind": "hermes-realtime", **options}}}, environ={})


def test_hermes_realtime_auth_notice_points_to_profile_login():
    from provider_failure import build_provider_failure

    notice = build_provider_failure("all_unavailable", {"accounts": [
        {"provider": "profile", "account": "default", "kind": "hermes-realtime", "reason": "auth_required"},
    ]})
    assert "hermes auth add openai-codex" in notice["message"]


def test_gpt_live_uses_default_openai_key_and_model_voice_settings():
    config = {"providers": {"live": {"kind": "gpt-live", "model": "gpt-live-1", "voice": "test-voice"}}}
    env = {}
    pool = LiveProviderPool(config, environ=env)
    assert pool.next_candidate() is None
    assert pool.status()["accounts"][0]["reason"] == "auth_required"

    env["OPENAI_API_KEY"] = "fixture-openai-credential"
    candidate = pool.next_candidate()
    assert (candidate.key, candidate.kind) == (("live", "default"), "gpt-live")
    assert (candidate.model, candidate.voice) == ("gpt-live-1", "test-voice")
    assert candidate.endpoint is None
    assert pool.secret(candidate) == env["OPENAI_API_KEY"]
    assert env["OPENAI_API_KEY"] not in repr(candidate)
    assert env["OPENAI_API_KEY"] not in json.dumps(pool.status())


def test_reconnect_requires_the_same_account_and_api_key():
    env = {"GEMINI_API_KEY": "fixture-key-1"}
    now = [10]
    pool = LiveProviderPool({"providers": {"gemini": {"kind": "gemini"}, "codex": {"kind": "codex"}}},
                            environ=env, clock=lambda: now[0])
    candidate = pool.next_candidate()
    pool.record_failure(candidate, "unavailable", retry_after=4)
    assert pool.reconnect_delay(candidate) == 4
    assert pool.next_candidate(only=candidate) is None

    now[0] += 4
    pool.set_selected("codex")
    assert pool.reconnect_delay(candidate) is None
    assert pool.next_candidate(only=candidate) is None

    pool.set_selected("gemini")
    assert pool.next_candidate(only=candidate).key == candidate.key
    env["GEMINI_API_KEY"] = "fixture-key-2"
    assert pool.reconnect_delay(candidate) is None
    assert pool.next_candidate(only=candidate) is None
    assert pool.next_candidate().key == candidate.key


@pytest.mark.parametrize("endpoint", [
    "wss://live.example.net/v1/live/sessions?project=demo",
    "ws://127.0.0.1:8799/gateway/live",
    "ws://[::1]:8799/gateway/live",
    "ws://localhost:8799/gateway/live",
])
def test_gpt_live_gateway_uses_its_own_account_and_preserves_rotation(endpoint, tmp_path):
    config = {
        "selected": "live",
        "providers": {
            "openai": {"kind": "gpt-live"},
            "gateway": {
                "kind": "gpt-live", "endpoint": endpoint,
                "accounts": {"project": {"secret_env": "GATEWAY_LIVE_KEY"}},
            },
        },
        "pools": {"live": ["gateway", "openai"]},
    }
    state = tmp_path / "rotation.json"
    env = {"OPENAI_API_KEY": "fixture-openai-key"}
    pool = LiveProviderPool(config, state, environ=env)
    assert pool.next_candidate("gateway") is None
    assert pool.status("gateway")["accounts"][0]["reason"] == "auth_required"
    direct = pool.next_candidate()
    assert direct.endpoint is None and pool.secret(direct) == env["OPENAI_API_KEY"]

    env["GATEWAY_LIVE_KEY"] = "fixture-gateway-key"
    gateway = pool.next_candidate()
    assert gateway.key == ("gateway", "project") and gateway.endpoint == endpoint
    assert pool.secret(gateway) == env["GATEWAY_LIVE_KEY"]
    assert pool.next_candidate().key == direct.key
    for output in (repr(gateway), json.dumps(pool.status()), state.read_text()):
        assert endpoint not in output
        assert env["GATEWAY_LIVE_KEY"] not in output


@pytest.mark.parametrize("endpoint", [
    None, "", "https://live.example.net/path", "ws://gateway.example.net/path", "wss:///path",
    "wss://user:fixture-private@live.example.net/path", "wss://live.example.net/path#fixture-private",
    "wss://live.example.net:/path", "wss://live.example.net:0/path", "wss://live.example.net:70000/path",
    "wss://live..example.net/path", "wss://prefix[::1]/path", "ws://127.0.0.1\\@gateway.example.net/path",
    "wss://live.example.net/fixture-private\npath", "wss://live.example.net/path with spaces",
])
def test_gpt_live_rejects_invalid_gateway_endpoint_without_echoing_it(endpoint):
    config = {"providers": {"gateway": {
        "kind": "gpt-live", "endpoint": endpoint, "accounts": {"project": {"secret_env": "GATEWAY_LIVE_KEY"}},
    }}}
    with pytest.raises(ValueError, match="Invalid GPT-Live WebSocket endpoint") as error:
        LiveProviderPool(config, environ={})
    assert "fixture-private" not in str(error.value)


def test_custom_gateway_needs_explicit_credential_and_gpt_live_protocol():
    gateway = {"kind": "gpt-live", "endpoint": "wss://live.example.net/path"}
    with pytest.raises(ValueError, match="explicit accounts"):
        LiveProviderPool({"providers": {"gateway": gateway}}, environ={"OPENAI_API_KEY": "fixture"})
    with pytest.raises(ValueError, match="requires kind gpt-live"):
        LiveProviderPool({"providers": {"gateway": {**gateway, "kind": "gemini"}}}, environ={})


def test_gpt_live_accounts_share_existing_pool_rotation_and_cooldown():
    config = live_config(1)
    config["providers"]["live"] = {
        "kind": "gpt-live", "accounts": {
            "primary": {"secret_env": "OPENAI_LIVE_PROJECT_A"},
            "backup": {"secret_env": "OPENAI_LIVE_PROJECT_B"},
        },
    }
    config["pools"]["daily"].append("live")
    env = {**live_secrets(1), "OPENAI_LIVE_PROJECT_A": "fixture-a", "OPENAI_LIVE_PROJECT_B": "fixture-b"}
    now = [10.0]
    pool = LiveProviderPool(config, environ=env, clock=lambda: now[0])
    first = [pool.next_candidate() for _ in range(3)]
    assert [candidate.key for candidate in first] == [
        ("subscription", "default"), ("speech", "g0"), ("live", "primary"),
    ]
    pool.record_failure(first[-1], "rate_limited", retry_after=20)
    assert [pool.next_candidate().key for _ in range(3)] == [
        ("subscription", "default"), ("speech", "g0"), ("live", "backup"),
    ]
    assert pool.next_candidate("live", exclude={("live", "backup")}) is None
    now[0] = 30.0
    assert pool.next_candidate("live").key == ("live", "primary")


def test_provider_and_account_rotation_have_distinct_cursors(tmp_path):
    state = tmp_path / "live.json"
    pool = LiveProviderPool(live_config(15), state, environ=live_secrets(15))
    attempts = [pool.next_candidate() for _ in range(30)]
    assert [candidate.provider for candidate in attempts[::2]] == ["subscription"] * 15
    gemini_attempts = attempts[1::2]
    assert [(candidate.provider, candidate.account) for candidate in gemini_attempts] == [
        ("speech", f"g{i}") for i in range(15)
    ]
    assert all(candidate.model == "models/gemini-live" and candidate.voice == "Aoede"
               for candidate in gemini_attempts)
    reopened = LiveProviderPool(live_config(15), state, environ=live_secrets(15))
    assert [reopened.next_candidate().key for _ in range(2)] == [
        ("subscription", "default"), ("speech", "g0"),
    ]
    reopened.set_selected("speech")
    assert reopened.next_candidate().account == "g1"
    assert reopened.next_candidate("daily").provider == "subscription"
    assert reopened.status()["selected"] == "speech"
    assert pool.secret(gemini_attempts[3]) == "fixture-credential-3"
    for output in (repr(gemini_attempts), json.dumps(pool.status()), state.read_text()):
        assert "fixture-credential-" not in output
    assert state.stat().st_mode & 0o777 == 0o600


def test_rotation_state_is_atomic_across_instances(tmp_path):
    state = tmp_path / "live.json"
    config, secrets = live_config(15), live_secrets(15)
    config["selected"] = "speech"
    pools = [LiveProviderPool(config, state, environ=secrets) for _ in range(15)]
    with ThreadPoolExecutor(max_workers=15) as executor:
        selected = list(executor.map(lambda pool: pool.next_candidate().account, pools))
    assert set(selected) == {f"g{i}" for i in range(15)}
    assert pools[0].next_candidate().account == "g0"


def test_attempt_skips_previous_candidates_and_unconfigured_keys():
    pool = LiveProviderPool(live_config(), environ=live_secrets(1))
    first = pool.next_candidate()
    second = pool.next_candidate(exclude={first.key})
    assert [first.key, second.key] == [("subscription", "default"), ("speech", "g0")]
    assert pool.next_candidate(exclude={first.key, second.key}) is None
    assert pool.status("speech")["accounts"][1]["reason"] == "auth_required"
    assert pool.status("speech")["reason"] is None


def test_quota_status_and_known_reset_honor_retry_after():
    now = [10.0]
    pool = LiveProviderPool(live_config(), environ=live_secrets(), clock=lambda: now[0])
    first, second, third = [pool.next_candidate() for _ in range(3)]
    assert [first.key, second.key, third.key] == [
        ("subscription", "default"), ("speech", "g0"), ("subscription", "default"),
    ]
    third = pool.next_candidate()
    assert third.key == ("speech", "g1")
    for candidate, reason, delay in (
        (first, "rate_limited", 30),
        (second, "rate_limited", 10),
        (third, "quota_exhausted", None),
    ):
        pool.record_failure(candidate, reason, retry_after=delay)
    assert pool.next_candidate() is None
    status = pool.status()
    assert status["reason"] == "all_rate_limited"
    assert status["retry_at"] == 20.0 and not status["retry_estimated"]
    now[0] = 19.999
    assert pool.next_candidate() is None
    now[0] = 20.0
    assert pool.next_candidate().key == second.key
    assert pool.status()["reason"] is None


@pytest.mark.parametrize("gemini_failure", ["access_denied", "auth_required"])
def test_mixed_failures_and_repaired_api_key(gemini_failure):
    secrets = live_secrets()
    pool = LiveProviderPool(live_config(), environ=secrets, clock=lambda: 10.0)
    first, second, third = [pool.next_candidate() for _ in range(3)]
    assert first.key == third.key
    third = pool.next_candidate()
    pool.record_failure(first, "auth_required")
    pool.record_failure(second, gemini_failure)
    pool.record_failure(third, "rate_limited")
    status = pool.status()
    assert status["reason"] == "all_unavailable"
    assert [account["reason"] for account in status["accounts"]] == [
        "auth_required", gemini_failure, "rate_limited",
    ]
    assert status["accounts"][1]["retry_at"] is None
    assert status["retry_estimated"] and 12.0 <= status["retry_at"] < 13.0
    secrets["LIVE_TEST_KEY_0"] = "repaired-credential"
    assert pool.secret(second) is None
    pool.record_failure(second, gemini_failure)
    repaired = pool.next_candidate()
    assert repaired.key == second.key and pool.secret(repaired) == "repaired-credential"
    pool.record_failure(repaired, "billing_required")
    assert pool.next_candidate() is None
    pool.record_ready(repaired)
    assert pool.next_candidate().key == repaired.key


def test_codex_login_can_be_rechecked_after_backoff():
    now = [10.0]
    pool = LiveProviderPool(environ={}, clock=lambda: now[0])
    codex = pool.next_candidate()
    pool.record_failure(codex, "auth_required")

    status = pool.status()
    assert status["reason"] == "all_unavailable"
    assert status["accounts"][0]["reason"] == "auth_required"
    assert status["retry_estimated"] and 12.0 <= status["retry_at"] < 13.0
    assert pool.next_candidate() is None

    now[0] = status["retry_at"]
    assert pool.next_candidate(exclude={codex.key}) is None
    retry = pool.next_candidate()
    assert retry.key == codex.key
    pool.record_failure(retry, "auth_required")
    retry_at = pool.status()["retry_at"]
    assert now[0] + 4 <= retry_at < now[0] + 5
    assert pool.next_candidate() is None

    now[0] = retry_at
    recovered = pool.next_candidate()
    assert recovered.key == codex.key
    pool.record_ready(recovered)
    assert pool.status()["accounts"][0]["reason"] is None
    pool.record_failure(recovered, "auth_required")
    assert now[0] + 2 <= pool.status()["retry_at"] < now[0] + 3


@pytest.mark.parametrize("change", [
    {"selected": "not-configured"},
    {"selected": {"account": "invalid"}},
    {"pools": {"daily": ["speech", "speech"]}},
    {"pools": {"daily": ["missing"]}},
    {"providers": {"x": {"kind": "gemini", "accounts": {"a": {"key": "private"}}}}},
    {"providers": {"x": {"kind": "gemini", "accounts": {"a": {"secret_env": "value-not-an-env"}}}}},
    {"providers": {"x": {"kind": "gpt-live", "accounts": {"a": {"key": "private"}}}}},
    {"providers": {"x": {"kind": "gpt-live", "accounts": {"a": {"secret_env": "value-not-an-env"}}}}},
    {"providers": {"x": {"kind": "codex", "accounts": {"a": {}, "b": {}}}}},
])
def test_rejects_unknown_provider_or_invalid_account_config(change):
    with pytest.raises(ValueError) as error:
        LiveProviderPool({**live_config(), **change}, environ={})
    assert "private" not in str(error.value)
