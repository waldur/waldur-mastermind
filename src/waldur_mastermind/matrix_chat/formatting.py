"""Text that bot messages interpolate, made safe for their Markdown."""

import re

# Markdown punctuation that can start a link, an image, emphasis or code within
# a line. Names in bot messages come from users, and the bot posts with power
# level 100. Only what one of the rules here needs is escaped: every escape
# shows as a backslash in the plain-text body that notifications and history
# exports display.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<])")
# A run of two or more. The formatted message only reads a run of tildes that
# starts it, as a code block that swallows the rest. Waldur's own chat renders
# the plain-text body instead, where a pair of these strikes through or
# highlights what lies between: with two names in one message, the bot's own
# words. A single ~ or = stays as it is.
_PAIRED_MARKER = re.compile(r"~{2,}|={2,}")
# Waldur's own chat links a bare URL and takes everything up to the next space
# or "<" into it, the backslashes of the escapes above included. A "<" after a
# URL is then no longer escaped, and "<!--" hides the bot's words up to a "-->"
# in the next name. With the scheme separator escaped, no link forms.
_URL_SCHEME = re.compile(r":(?=//)")
# Some bot messages start with a user's name, where these open a heading, a
# quote or a list.
_BLOCK_MARKER = re.compile(r"^(\s*)([#>+-])")
# Only before a space: "2.0 migration" is no list.
_LIST_NUMBER = re.compile(r"^(\s*\d+)([.)])(?=\s|$)")
_WHITESPACE = re.compile(r"\s+")


def _one_line(text) -> str:
    # After a line break the next line can open a block of its own: a quote, a
    # heading, a list, or a link once a code span is cut short.
    # Every other kind of whitespace goes too. Waldur's own chat deletes a form
    # feed before it parses, which would join "~\f~" into a pair after the
    # checks below had let it through.
    return _WHITESPACE.sub(" ", str(text))


def escape_markdown(text) -> str:
    """Text that renders literally in a bot message.

    Keep a bolded name the only bold or emphasis on its line. Waldur's own chat
    reads the backslashes of a name ending in one as escaping the closing "**",
    and the bold then runs on to the next one.
    """
    text = _MARKDOWN_SPECIAL.sub(r"\\\1", _one_line(text))
    text = _PAIRED_MARKER.sub(lambda run: "\\" + "\\".join(run.group()), text)
    text = _URL_SCHEME.sub(r"\\:", text)
    text = _BLOCK_MARKER.sub(r"\1\\\2", text)
    return _LIST_NUMBER.sub(r"\1\\\2", text)


def code_span(text) -> str:
    """Text shown as inline code; a backtick inside would end the span early."""
    text = _one_line(text).replace("`", "'")
    # Waldur's own chat reads a backslash before the closing backtick as
    # escaping it, and an empty span as the start of a longer one. Either way
    # the span runs on into the text after it, and a name there is rendered.
    # A space at each end keeps the span closed; the formatted message drops
    # the pair again, unless the name was empty.
    if not text or text.endswith("\\"):
        text = f" {text} "
    return f"`{text}`"
