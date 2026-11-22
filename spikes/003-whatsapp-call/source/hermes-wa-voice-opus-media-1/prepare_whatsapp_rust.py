"""Prepare the pinned WhatsApp dependency with the reviewed voice patches."""

import errno
import hashlib
from pathlib import Path
import subprocess
import tempfile


UPSTREAM = "https://github.com/oxidezap/whatsapp-rust.git"
REVISION = "9b9fdb7e5c4211cdc7c837abb6a9ae9c597e35c5"
PATCHES = (
    "whatsapp-rust-playout.patch",
    "whatsapp-rust-relay-failover.patch",
    "whatsapp-rust-before-accept.patch",
    "whatsapp-rust-owner-lid-proof.patch",
)
PATCHED_SHA256 = {
    "src/client/lid_pn.rs": "7fcf7b25f064c1687332ff205fb12b553d5db24977b270a3e9077d3ab63999bc",
    "src/handlers/call.rs": "19da32e7ba8ac526fd84415d651b142bf579667ccff393f434920103232727d0",
    "src/voip/facade.rs": "ee59c19e2dcbb0879139db67a951378785585f90708568b67483f51478e27747",
    "src/voip_control/wacore_backend.rs": "1aa8bed29e8cd559ebc8441c4509b40e83462831683d9fce32d6bcd6af547fa9",
    "wacore/src/voip/engine.rs": "f6ad41d77eb8e616fcede7fe780b86f491797b35bcad382d7112b4c4bd4c5404",
    "wacore/src/voip_control/engine_bridge.rs": "3f86002c5ba4c96ab68c04fd1b752935754f61803650ddaff26ef33ef6795145",
    "wacore/src/voip_control/mod.rs": "0094547db4e198041129eb371fe003ee418904c2764327373c48f87634cf6725",
    "wacore/src/voip_control/spec_build.rs": "135310a87b23c664db71f3acb605348ccb3ddf8d692056bb08eb76b6f30a9f54",
    "wacore/src/voip_control/transport.rs": "c442def72bbb00ef8ddcd0f92deebe885bc7670c61366fd8ee7601b6e3848cd0",
}


def git(directory, *args, capture=False):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def verify(directory):
    revision = git(directory, "rev-parse", "HEAD", capture=True).strip()
    changes = git(directory, "status", "--porcelain", "--untracked-files=all", capture=True).splitlines()
    expected_changes = [f" M {name}" for name in sorted(PATCHED_SHA256)]
    if revision != REVISION or changes != expected_changes or any(
        hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest
        for name, digest in PATCHED_SHA256.items()
    ):
        raise SystemExit(f"Unexpected WhatsApp source in {directory}")


def main():
    source = Path(__file__).resolve().parent
    build = source.parents[1] / ".build"
    build.mkdir(exist_ok=True)
    destination = build / "whatsapp-rust-relays"
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix="whatsapp-rust-", dir=build) as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            git(checkout, "init", "--quiet")
            git(checkout, "fetch", "--depth=1", "--no-tags", UPSTREAM, REVISION)
            git(checkout, "checkout", "--quiet", "--detach", REVISION)
            for patch in PATCHES:
                git(checkout, "apply", "--unidiff-zero", str(source / patch))
            verify(checkout)
            try:
                checkout.rename(destination)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    verify(destination)
    print(destination)


if __name__ == "__main__":
    main()
