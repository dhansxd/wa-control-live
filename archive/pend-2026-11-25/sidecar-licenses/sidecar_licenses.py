"""Reproduce the historical sidecar dependency notices from a prepared Cargo registry."""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import os
import re
import tomllib


ARCHIVE = Path(__file__).resolve().parent
ROOT = Path.cwd()
REGISTRY = Path(os.environ["CRATE_REGISTRY_SRC"]).resolve()
UPSTREAM = ROOT / ".build/whatsapp-rust-relays"
WORKSPACE = ROOT / "source/hermes-wa-voice-opus-media-1"
PINNED_NOTICES = ARCHIVE / "pinned"


@dataclass(frozen=True)
class Crate:
    name: str
    version: str
    license: str
    location: Path

    @property
    def label(self):
        return f"{self.name} {self.version}"


def production_crates():
    by_name = {}
    for line in (ARCHIVE / "production-crates.txt").read_text().splitlines():
        label, license_expression = line.rsplit("|", maxsplit=1)
        name, version, *_ = label.split(maxsplit=2)
        version = version.removeprefix("v")
        local = re.search(r" \((\./[^)]*)\)$", label)
        if local:
            location = ROOT / local.group(1)
        else:
            location = REGISTRY / f"{name}-{version}"
        by_name[name, version] = Crate(name, version, license_expression, location)
    return sorted(by_name.values(), key=lambda crate: crate.label)


def candidate_paths(crate):
    def basenames(directory):
        return sorted(
            (path for path in directory.iterdir() if path.name.upper().startswith(
                ("LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE", "PATENT", "AUTHORS"))),
            key=lambda path: path.name
        )

    paths = basenames(crate.location)
    manifest = tomllib.loads((crate.location / "Cargo.toml").read_text())
    relative = manifest["package"].get("license-file")
    if relative:
        paths.append(crate.location / relative)
    return sorted(set(
        file
        for path in paths if path.exists()
        for file in (path.rglob("*") if path.is_dir() else [path])
        if file.is_file()
    ))


def notices(crate):
    paths = candidate_paths(crate)
    if crate.location == WORKSPACE:
        return []
    if UPSTREAM in crate.location.parents or crate.location == UPSTREAM:
        return [(UPSTREAM / "LICENSE").read_text()]
    if crate.name == "asn1-rs-impl":
        return [(PINNED_NOTICES / "asn1-rs-impl/LICENSE-MIT").read_text()]
    if crate.name.startswith("rtc-") and not paths:
        release = "rtc-beta" if "-beta." in crate.version else "rtc-current"
        return [(PINNED_NOTICES / release / "LICENSE-MIT").read_text()]
    if crate.name == "crc32c":
        zlib_notice = (PINNED_NOTICES / "crc32c/zlib-notice.txt").read_text().rstrip("\n")
        return [
            "crc32c by Zack Owens. The upstream README grants Apache-2.0 or MIT.\n"
            "MIT terms are used for this distribution.\n\n" + (REGISTRY / "ident_case-1.0.1/LICENSE").read_text(),
            "crc32c translates the Zlib crc32.c combined-checksum function to Rust,\n"
            "with a CRC32c polynomial. Original C implementation:\n"
            "Copyright (C) 1995-2006, 2010, 2011, 2012, 2016 Mark Adler\n\n"
            + zlib_notice,
        ]
    if crate.name == "ring":
        paths.extend([
            crate.location / "third_party/fiat/LICENSE",
            crate.location / "src/polyfill/once_cell/LICENSE-MIT",
        ])
        return [path.read_text() for path in paths]
    if crate.name == "opusic-sys":
        paths.append(crate.location / "opus/COPYING")
    if crate.name == "libsqlite3-sys":
        paths.append(crate.location / "sqlite3/sqlite3.c")
    if "MIT" in crate.license and (" OR " in crate.license or "/" in crate.license):
        mit = [path for path in paths if path.name.upper().startswith("LICENSE-MIT")]
        if mit:
            paths = mit + [
                path for path in paths if path.name.upper().startswith(
                    ("COPYRIGHT", "NOTICE", "AUTHORS", "LICENSE-THIRD-PARTY", "LICENSE-UNICODE")
                )
            ]
    if crate.license == "Apache-2.0 OR BSL-1.0":
        paths = [path for path in paths if "BOOST" not in path.name.upper()]
    result = []
    for path in paths:
        notice = path.read_text()
        if path.name == "sqlite3.c":
            notice = notice.split("** The author disclaims copyright", maxsplit=1)[1].split("**", maxsplit=1)[0]
            notice = "The author disclaims copyright" + notice
            notice += "\nSQLite is in the public domain. https://sqlite.org/copyright.html"
        result.append(notice)
    if not result:
        raise ValueError(f"No distribution notice for {crate.label}")
    return result


def render():
    canonical = {}
    groups = defaultdict(set)
    for crate in production_crates():
        for notice in notices(crate):
            normalized = " ".join(notice.split())
            canonical.setdefault(normalized, notice.strip())
            groups[normalized].add(crate.label)
    header = (
        "Third-party licensing notices for the WA Control & Live macOS ARM64 sidecar\n"
        "Cargo.lock is the package and version inventory for the native release.\n"
        "Identical notices are grouped by package. Rust's standard library notices\n"
        "are in RUST-STANDARD-LIBRARY-COPYRIGHT.html alongside this file.\n"
    )
    sections = [header]
    for key, crates in sorted(groups.items(), key=lambda item: (sorted(item[1]), item[0])):
        names = sorted(crates)
        sections.append("\n".join(["=" * 76, "Applies to:", *names, "", canonical[key]]))
    return "\n\n".join(sections) + "\n"


if __name__ == "__main__":
    print(render(), end="")
