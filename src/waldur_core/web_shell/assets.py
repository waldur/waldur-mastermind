"""ghostty-web, the browser terminal: a prebuilt tarball, pinned by hash.

The tarball comes from the Waldur fork (github.com/waldur/ghostty-web),
which carries security fixes pending upstream review: control characters in
pastes (CVE-2026-26982), http(s)-only hyperlinks, WASM loading under a strict
CSP, window-title sanitising, zeroed WASM pages, ignored ESC k titles and
coherent viewport reads. Its release workflow builds the npm tarball, so it is
downloaded as built rather than built here; Mastermind has no Node or Zig
toolchain to build it with.
"""

import base64
import hashlib
import io
import os
import tarfile
from pathlib import Path

import httpx

GHOSTTY_WEB_VERSION = "0.4.0-waldur.2"
TARBALL_URL = (
    "https://github.com/waldur/ghostty-web/releases/download/"
    f"waldur-v{GHOSTTY_WEB_VERSION}/ghostty-web-{GHOSTTY_WEB_VERSION}.tgz"
)
# npm integrity of the release tarball, as recorded in the release notes.
INTEGRITY = "sha512-kS7CjE5nrd5yM7y+Jk3KSM9Eg6uT/tfKHntdhDvIanmS4MtIKCecdQEWa1/WKo8s0LH1Z74+V6B8hemLXBu9oQ=="
REQUIRED_FILES = ("dist/ghostty-web.js",)
# The WASM is inlined in dist/ghostty-web.js and decoded in the browser, so the
# separate .wasm files, like the type declarations and the CommonJS build, are
# never served.
SKIPPED_SUFFIXES = (".d.ts", ".umd.cjs", ".wasm")


def assets_dir() -> Path:
    override = os.environ.get("WALDUR_WEB_SHELL_ASSETS_DIR")
    if override:
        return Path(override)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "waldur-web-shell" / f"ghostty-web-{GHOSTTY_WEB_VERSION}"


def is_installed(path: Path | None = None) -> bool:
    path = path or assets_dir()
    return all((path / name).is_file() for name in REQUIRED_FILES)


def fetch(path: Path | None = None) -> Path:
    path = (path or assets_dir()).resolve()
    response = httpx.get(TARBALL_URL, follow_redirects=True, timeout=60)
    response.raise_for_status()
    digest = hashlib.sha512(response.content).digest()
    integrity = "sha512-" + base64.b64encode(digest).decode()
    if integrity != INTEGRITY:
        raise ValueError(
            f"ghostty-web tarball does not match the pinned hash: {integrity}"
        )

    path.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith("package/"):
                continue
            relative = member.name.removeprefix("package/")
            if relative.endswith(SKIPPED_SUFFIXES):
                continue
            target = (path / relative).resolve()
            if not target.is_relative_to(path):
                raise ValueError(f"Refusing to extract outside {path}: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    return path
