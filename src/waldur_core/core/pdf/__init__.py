"""PDF rendering with Typst and the fonts bundled with Waldur.

A template gets its data as JSON in `sys.inputs.data` and inserts the values as
text, never as markup, so they print literally whatever characters they hold.
Templates import no packages, and Typst reads no file outside the template's
directory, so rendering never touches the network.

Bundled Source Sans 3 covers Latin, Cyrillic and Greek. Host fonts are only a
fallback for scripts it lacks (CJK, Arabic, Hebrew, ...).
"""

import json
import os
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import threading
from pathlib import Path
from typing import cast

import typst

FONTS_DIR = Path(__file__).parent / "fonts"
EXPORT_TEMPLATE = Path(__file__).parent / "export.typ"
WORKER = Path(__file__).parent / "worker.py"

# A child gets this much address space. The largest export the API accepts
# peaks at about 700 MB resident.
ISOLATED_MEMORY_LIMIT = 2 * 1024**3
# Well below gunicorn's 30 second worker timeout, so the request fails
# cleanly instead of the worker being killed. The largest export the API
# accepts takes a few seconds.
ISOLATED_TIMEOUT = 15

_fonts = None
_fonts_lock = threading.Lock()


class PdfRenderError(Exception):
    """The child could not render the document: Typst rejected its input,
    or it ran out of time or memory."""


class PdfRenderTimeout(PdfRenderError):
    pass


def get_fonts() -> typst.Fonts:
    """Scan the bundled and host fonts once per process.

    Scanning takes far longer than compiling one document. A typst.Compiler
    cannot be used from two threads at once, but compilers built on one Fonts
    can, and building one is cheap once the fonts are scanned.
    """
    global _fonts
    with _fonts_lock:
        if _fonts is None:
            _fonts = typst.Fonts(font_paths=[str(FONTS_DIR)])
        return _fonts


def render_pdf(template: Path, data) -> bytes:
    """Render a document of bounded size, such as an invoice, in this process."""
    compiler = typst.Compiler(
        str(template), root=str(template.parent), font_paths=get_fonts()
    )
    # A PDF is one document; only PNG and SVG output come back per page.
    return cast(bytes, compiler.compile(sys_inputs={"data": json.dumps(data)}))


def render_pdf_isolated(
    template: Path, data, files: dict[str, bytes] | None = None
) -> bytes:
    """Render a document whose size the client controls in a child process.

    Typst keeps what it laid out in a process-wide cache for the next several
    compiles, and a table of tens of thousands of cells takes hundreds of
    megabytes. A long-lived API worker would hold on to that; the child gives
    it all back when it exits. It also runs under ISOLATED_MEMORY_LIMIT and is
    killed after ISOLATED_TIMEOUT seconds.

    `files` are written next to a copy of the template, so it reads them by
    name, e.g. `image("chart.png")`. Their names come from the caller, never
    from the client.
    """
    with tempfile.TemporaryDirectory() as root:
        main = Path(root) / template.name
        shutil.copyfile(template, main)
        for name, content in (files or {}).items():
            (Path(root) / name).write_bytes(content)
        # The image preloads jemalloc for the long-lived workers. A child that
        # exits after one document gains nothing from it, and its reserved
        # address space counts against the limit.
        env = {key: value for key, value in os.environ.items() if key != "LD_PRELOAD"}
        # Typst lays out on a thread per core, and glibc reserves an arena of
        # address space per thread: on a many-core node that alone would
        # exhaust ISOLATED_MEMORY_LIMIT before any document data.
        env.update(RAYON_NUM_THREADS="2", MALLOC_ARENA_MAX="2")
        try:
            result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-I",
                    str(WORKER),
                    str(main),
                    str(FONTS_DIR),
                    str(ISOLATED_MEMORY_LIMIT),
                ],
                input=json.dumps(data).encode(),
                capture_output=True,
                timeout=ISOLATED_TIMEOUT,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfRenderTimeout(
                f"PDF rendering took longer than {ISOLATED_TIMEOUT} seconds"
            ) from e
    if result.returncode:
        raise PdfRenderError(
            f"PDF rendering failed with exit code {result.returncode}: "
            f"{result.stderr.decode(errors='replace')[-2000:]}"
        )
    return result.stdout
