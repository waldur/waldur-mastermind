"""Render one PDF in a child process for `render_pdf_isolated`.

Run as a script, never imported: the arguments are the template, the fonts
directory and the memory limit in bytes, the JSON data arrives on stdin and the
PDF leaves on stdout. It imports nothing from Waldur, so it starts in a
fraction of a second.
"""

import os
import resource
import sys
from typing import cast

import typst


def main():
    template, fonts_dir, memory_limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    except (ValueError, OSError):
        # macOS does not support an address-space limit; Linux does.
        pass
    compiler = typst.Compiler(
        template,
        root=os.path.dirname(template),
        font_paths=typst.Fonts(font_paths=[fonts_dir]),
    )
    content = compiler.compile(sys_inputs={"data": sys.stdin.read()})
    sys.stdout.buffer.write(cast(bytes, content))


if __name__ == "__main__":
    main()
