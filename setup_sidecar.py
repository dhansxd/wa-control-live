"""Verify the bundled sidecar or build one from the pinned source."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


_BUILD_FILES = (
    "Cargo.toml",
    "Cargo.lock",
    "prepare_whatsapp_rust.py",
    "whatsapp-rust-before-accept.patch",
    "whatsapp-rust-owner-lid-proof.patch",
    "whatsapp-rust-playout.patch",
    "whatsapp-rust-relay-failover.patch",
)
_TARGET = "aarch64-apple-darwin"


def _build_files(source):
    rust = source / "src"
    if rust.is_symlink() or not (rust / "main.rs").is_file():
        raise RuntimeError("The installed plugin is missing its sidecar source.")
    files = [source / name for name in _BUILD_FILES] + sorted(rust.rglob("*.rs"))
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise RuntimeError("The installed plugin has incomplete sidecar build files.")
    return files


def source_checksum(source):
    """Identify the installed sources that setup builds and can later reuse."""
    digest = hashlib.sha256()
    for path in _build_files(source):
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def packaged_sidecar(source: Path) -> tuple[Path, str]:
    """Verify the release binary against the installed plugin revision."""
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise RuntimeError("The bundled WhatsApp sidecar requires macOS on Apple Silicon.")
    directory = Path(__file__).resolve().parent / "sidecar" / "darwin-arm64"
    binary = directory / "hermes-wa-voice"
    manifest = directory / "build.json"
    if any(path.is_symlink() for path in (directory.parent, directory, binary, manifest)):
        raise RuntimeError("The bundled WhatsApp sidecar must use regular files in the plugin package.")
    if not manifest.is_file() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("The bundled WhatsApp sidecar is incomplete. Reinstall WA Control & Live.")
    try:
        build = json.loads(manifest.read_text(encoding="utf-8"))
        with binary.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise RuntimeError("Could not read the bundled WhatsApp sidecar or its checksum manifest.") from exc
    if not isinstance(build, dict) or build.get("binary_sha256") != checksum:
        raise RuntimeError("The bundled WhatsApp sidecar checksum does not match its manifest.")
    if build.get("source_sha256") != source_checksum(source):
        raise RuntimeError("The bundled WhatsApp sidecar source checksum does not match the installed source.")
    return binary, checksum


@contextmanager
def build_sidecar(source: Path, *, state: Path):
    """Yield a native release binary while its private build tree exists."""
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise RuntimeError("Building the WhatsApp sidecar requires macOS on Apple Silicon.")
    cargo = shutil.which("cargo")
    if not cargo or not shutil.which("rustc") or not shutil.which("git"):
        raise RuntimeError("Install Rust 1.94 or later and Git, then run setup again.")
    if not shutil.which("cmake"):
        raise RuntimeError("Install CMake to build the bundled Opus library, then run setup again.")
    clang = subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True)
    if clang.returncode:
        raise RuntimeError("Install the Xcode Command Line Tools with xcode-select --install, then run setup again.")

    files = _build_files(source)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or not state.is_dir() or state.stat().st_uid != os.getuid():
        raise ValueError("The sidecar build directory must belong to this profile.")
    with TemporaryDirectory(prefix="sidecar-build-", dir=state) as directory:
        root = Path(directory)
        # Both the preparation script and Cargo expect .build two levels above the crate.
        crate = root / "source" / source.name
        for path in files:
            staged = crate / path.relative_to(source)
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, staged)

        print("Preparing the pinned WhatsApp Rust dependency…", flush=True)
        try:
            subprocess.run([sys.executable, "-I", str(crate / "prepare_whatsapp_rust.py")], cwd=crate, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Could not prepare the pinned WhatsApp dependency. Check Git and network access.") from exc

        print("Building the WhatsApp sidecar from source…", flush=True)
        target_dir = root / "target"
        try:
            subprocess.run([cargo, "build", "--locked", "--release", "--target", _TARGET,
                            "--target-dir", str(target_dir)], cwd=crate, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Could not build the sidecar. Check the Cargo output and Rust toolchain.") from exc
        binary = target_dir / _TARGET / "release" / "hermes-wa-voice"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Cargo did not produce an executable WhatsApp sidecar.")
        yield binary
