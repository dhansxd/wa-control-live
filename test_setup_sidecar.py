"""Check that public setup only accepts the binary bundled for its source."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("fixture_setup_sidecar", ROOT / "setup_sidecar.py")
sidecar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sidecar)


@pytest.fixture
def apple_silicon(monkeypatch):
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.platform, "machine", lambda: "arm64")


@pytest.fixture
def bundle(apple_silicon, monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar, "__file__", str(tmp_path / "setup_sidecar.py"))
    source = tmp_path / "source"
    rust = source / "src"
    rust.mkdir(parents=True)
    (rust / "main.rs").write_text("fn main() {}", encoding="utf-8")
    for name in sidecar._BUILD_FILES:
        (source / name).write_text(name, encoding="utf-8")
    directory = tmp_path / "sidecar" / "darwin-arm64"
    directory.mkdir(parents=True)
    binary = directory / "hermes-wa-voice"
    binary.write_bytes(b"stand-in sidecar")
    binary.chmod(0o755)
    manifest = directory / "build.json"
    manifest.write_text(json.dumps({
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "source_sha256": sidecar.source_checksum(source),
    }), encoding="utf-8")
    return source, binary, manifest


def test_repository_bundle_matches_source(apple_silicon):
    source = ROOT / "spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1"
    binary, checksum = sidecar.packaged_sidecar(source)
    assert binary == ROOT / "sidecar/darwin-arm64/hermes-wa-voice"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == checksum


def test_rejects_modified_binary(bundle):
    source, binary, _manifest = bundle
    binary.write_bytes(b"wrong executable")
    with pytest.raises(RuntimeError, match="checksum does not match its manifest"):
        sidecar.packaged_sidecar(source)


def test_rejects_binary_from_other_source_revision(bundle):
    source, _binary, _manifest = bundle
    (source / "src" / "main.rs").write_text("fn main() { todo!(); }", encoding="utf-8")
    with pytest.raises(RuntimeError, match="source checksum does not match"):
        sidecar.packaged_sidecar(source)


def test_rejects_incomplete_bundle(bundle):
    source, binary, _manifest = bundle
    binary.chmod(0o644)
    with pytest.raises(RuntimeError, match="incomplete"):
        sidecar.packaged_sidecar(source)


def test_rejects_invalid_manifest(bundle):
    source, _binary, manifest = bundle
    manifest.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum manifest"):
        sidecar.packaged_sidecar(source)


def test_rejects_unsupported_platform(monkeypatch, apple_silicon):
    monkeypatch.setattr(sidecar.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="requires macOS on Apple Silicon"):
        sidecar.packaged_sidecar(ROOT)
