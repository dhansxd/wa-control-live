"""Choose a provider and account for one Live audio connection."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
from ipaddress import ip_address
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit


_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_URL_AUTHORITY = re.compile(r"(?:\[[^\]]+\]|[^:\[\]]+)(?::[0-9]+)?")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)
_API_KEY_ENV = {"gemini": "GEMINI_API_KEY", "gpt-live": "OPENAI_API_KEY"}
_LOGIN_KINDS = ("codex", "hermes-realtime")
_LIMITS = {"rate_limited", "quota_exhausted"}
_TRANSIENT = {"rate_limited", "unavailable", "unknown"}
_FAILURE_REASONS = _TRANSIENT | _LIMITS | {
    "billing_required", "auth_required", "access_denied", "unsupported",
}


def _mapping(value, fields, name):
    if not isinstance(value, dict) or value.keys() - fields:
        raise ValueError(f"Invalid {name} settings")
    return value


def _websocket_endpoint(value):
    error = "Invalid GPT-Live WebSocket endpoint"
    if (not isinstance(value, str) or "\\" in value or "#" in value
            or any(char.isspace() or not char.isprintable() for char in value)):
        raise ValueError(error)
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
        if (url.scheme not in ("ws", "wss") or not host or port == 0 or "%" in host
                or url.username is not None or not _URL_AUTHORITY.fullmatch(url.netloc)):
            raise ValueError(error)
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            labels = host.encode("idna").decode("ascii").removesuffix(".")
            if (url.netloc.startswith("[") or len(labels) > 253
                    or not all(_HOST_LABEL.fullmatch(label) for label in labels.split("."))):
                raise ValueError(error)
            loopback = host == "localhost"
        if url.scheme == "ws" and not loopback:
            raise ValueError(error)
    except ValueError:
        raise ValueError(error) from None
    return value


def _rotate(values, last):
    offset = values.index(last) + 1 if last in values else 0
    return values[offset:] + values[:offset]


@dataclass(frozen=True)
class LiveCandidate:
    provider: str
    account: str
    kind: str
    model: str | None
    voice: str | None
    secret_env: str | None = None
    endpoint: str | None = field(default=None, repr=False)
    _credential: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def key(self):
        return self.provider, self.account


@dataclass(frozen=True)
class _Failure:
    reason: str
    retry_at: float | None = None
    retry_estimated: bool = False
    count: int = 1
    credential: bytes | None = None


class LiveProviderPool:
    def __init__(self, live_config=None, state_path=None, *, environ=None, clock=time.time):
        if live_config is None or live_config == {}:
            live_config = {"selected": "codex", "providers": {"codex": {"kind": "codex"}}}
        config = _mapping(live_config, {"selected", "providers", "pools"}, "Live")
        providers, pools = config.get("providers"), config.get("pools", {})
        if not isinstance(providers, dict) or not providers or not isinstance(pools, dict):
            raise ValueError("Configure at least one Live provider")
        self.providers = {alias: self._accounts(alias, entry) for alias, entry in providers.items()}
        self.pools = {}
        for alias, order in pools.items():
            if (not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in self.providers
                    or not isinstance(order, list) or not order
                    or any(not isinstance(name, str) or name not in self.providers for name in order)
                    or len(set(order)) != len(order)):
                raise ValueError("Invalid Live provider pool")
            self.pools[alias] = tuple(order)
        selected = config.get("selected", next(iter(self.providers)))
        self._provider_order(selected)
        self.selected = selected
        self.state_path = Path(state_path) if state_path is not None else None
        self._state = {"pools": {}, "accounts": {}}
        self._failures = {}
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def _accounts(alias, value):
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise ValueError("Invalid Live provider alias")
        provider = _mapping(value, {"kind", "model", "voice", "accounts", "endpoint"}, "Live provider")
        kind, model, voice = provider.get("kind"), provider.get("model"), provider.get("voice")
        if kind not in (*_LOGIN_KINDS, *_API_KEY_ENV):
            raise ValueError("Unsupported Live provider")
        if any(v is not None and (not isinstance(v, str) or not v.strip()) for v in (model, voice)):
            raise ValueError("Invalid Live model or voice")
        if kind == "codex" and model is not None:
            raise ValueError("The Codex audio model is selected by the current Realtime route")
        endpoint = None
        if "endpoint" in provider:
            if kind != "gpt-live":
                raise ValueError("A custom Live endpoint requires kind gpt-live")
            endpoint = _websocket_endpoint(provider["endpoint"])
            if "accounts" not in provider:
                raise ValueError("A custom Live endpoint needs explicit accounts with secret_env")
        default = {"secret_env": _API_KEY_ENV[kind]} if kind in _API_KEY_ENV else {}
        accounts = provider.get("accounts", {"default": default})
        if not isinstance(accounts, dict) or not accounts or (kind in _LOGIN_KINDS and len(accounts) != 1):
            raise ValueError("Invalid Live accounts")
        result = {}
        for name, entry in accounts.items():
            if not isinstance(name, str) or not _ALIAS.fullmatch(name):
                raise ValueError("Invalid Live account alias")
            entry = _mapping(entry, default.keys(), "Live account")
            secret_env = entry.get("secret_env")
            if default and (not isinstance(secret_env, str) or not _ENV_NAME.fullmatch(secret_env)):
                raise ValueError("Live account needs an environment variable reference")
            result[name] = LiveCandidate(alias, name, kind, model, voice, secret_env, endpoint=endpoint)
        return result

    def _provider_order(self, selection):
        if isinstance(selection, str):
            if selection in self.providers:
                return (selection,)
            if selection in self.pools:
                return self.pools[selection]
        raise ValueError("Unknown Live provider or pool")

    def set_selected(self, alias):
        self._provider_order(alias)
        with self._lock:
            self.selected = alias

    def _secret_value(self, candidate):
        value = self._environ.get(candidate.secret_env) if candidate.secret_env else None
        return value.strip() or None if isinstance(value, str) else None

    @staticmethod
    def _fingerprint(secret):
        return hashlib.sha256(secret.encode()).digest() if secret is not None else None

    def secret(self, candidate):
        value = self._secret_value(candidate)
        return value if self._fingerprint(value) == candidate._credential else None

    def _blocked(self, candidate, now):
        if candidate.secret_env and candidate._credential is None:
            return _Failure("auth_required")
        failure = self._failures.get(candidate.key)
        if (failure and failure.credential == candidate._credential
                and (failure.retry_at is None or failure.retry_at > now)):
            return failure
        return None

    @contextmanager
    def _load_state(self):
        if self.state_path is None:
            yield self._state
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.state_path.read_text())
            except FileNotFoundError:
                state = {"pools": {}, "accounts": {}}
            if (not isinstance(state, dict) or set(state) != {"pools", "accounts"}
                    or any(not isinstance(item, dict) for item in state.values())):
                raise ValueError("Invalid Live rotation state")
            yield state

    def _save_state(self, state):
        if self.state_path is None:
            return
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=self.state_path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(state, file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(temporary, self.state_path)
            finally:
                temporary.unlink(missing_ok=True)

    def _with_credential(self, candidate):
        return replace(candidate, _credential=self._fingerprint(self._secret_value(candidate)))

    def next_candidate(self, selection=None, *, exclude=(), only=None):
        """Reserve the next available account. Pass earlier attempts as keys."""
        excluded = set(exclude)
        with self._lock, self._load_state() as state:
            selection = self.selected if selection is None else selection
            order = _rotate(self._provider_order(selection), state["pools"].get(selection))
            now = self._clock()
            for provider in order:
                accounts = self.providers[provider]
                for account in _rotate(tuple(accounts), state["accounts"].get(provider)):
                    candidate = self._with_credential(accounts[account])
                    if only is not None and (candidate.key != only.key or candidate._credential != only._credential):
                        continue
                    if candidate.key in excluded or self._blocked(candidate, now):
                        continue
                    if selection in self.pools:
                        state["pools"][selection] = provider
                    state["accounts"][provider] = account
                    self._save_state(state)
                    return candidate
        return None

    def reconnect_delay(self, candidate):
        """Return the remaining cooldown for retrying a disconnected account."""
        with self._lock:
            if candidate.provider not in self._provider_order(self.selected):
                return None
            failure = self._failures.get(candidate.key)
            if (failure is None or failure.reason not in _TRANSIENT - _LIMITS or failure.retry_at is None
                    or failure.credential != candidate._credential
                    or self._with_credential(candidate)._credential != candidate._credential):
                return None
            return max(0, failure.retry_at - self._clock())

    def record_failure(self, candidate, reason, *, retry_after=None):
        if reason not in _FAILURE_REASONS:
            raise ValueError("Unknown Live connection failure")
        if retry_after is not None and (type(retry_after) not in (int, float)
                                        or not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("Invalid Live retry delay")
        with self._lock:
            credential = self._with_credential(candidate)._credential
            if credential != candidate._credential:
                return
            previous = self._failures.get(candidate.key)
            count = (min(6, previous.count + 1) if previous and previous.reason == reason
                     and previous.credential == credential else 1)
            # A fresh connection can recheck the provider's login without an API key change.
            retry_login = candidate.kind in _LOGIN_KINDS and reason == "auth_required"
            estimated = retry_after is None and (reason in _TRANSIENT or retry_login)
            if estimated:
                retry_after = min(60, 2 ** count + random.random())
            retry_at = None if retry_after is None else self._clock() + retry_after
            self._failures[candidate.key] = _Failure(reason, retry_at, estimated, count, credential)

    def record_ready(self, candidate):
        with self._lock:
            failure = self._failures.get(candidate.key)
            if failure and failure.credential == candidate._credential:
                del self._failures[candidate.key]

    def status(self, selection=None):
        """Summarize available accounts and retry timing without credential data."""
        with self._lock:
            selection = self.selected if selection is None else selection
            now = self._clock()
            accounts = []
            for provider in self._provider_order(selection):
                for config in self.providers[provider].values():
                    candidate = self._with_credential(config)
                    failure = self._blocked(candidate, now)
                    accounts.append({
                        "provider": provider, "account": candidate.account, "kind": candidate.kind,
                        "reason": failure.reason if failure else None,
                        "retry_at": failure.retry_at if failure else None,
                        "retry_estimated": failure.retry_estimated if failure else False,
                    })
        reason = None
        next_retry = None
        if all(account["reason"] for account in accounts):
            reason = ("all_rate_limited" if all(account["reason"] in _LIMITS for account in accounts)
                      else "all_unavailable")
            timed = (account for account in accounts if account["retry_at"] is not None)
            next_retry = min(timed, key=lambda account: (account["retry_at"], account["retry_estimated"]),
                             default=None)
        return {
            "selected": selection, "reason": reason, "accounts": accounts,
            "retry_at": next_retry["retry_at"] if next_retry else None,
            "retry_estimated": next_retry["retry_estimated"] if next_retry else False,
        }
