import nh3

ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "code",
    "em",
    "i",
    "li",
    "ol",
    "strong",
    "ul",
    "p",
    "span",
    "u",
    "s",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "sub",
    "sup",
    "pre",
    "br",
}


ALLOWED_ATTRIBUTES = {
    "a": {"href"},
    "p": {"style"},
    "span": {"style"},
}


def clean_html(value):
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
    )


def unescape_html(value):
    return value.replace("&lt;", "<").replace("&gt;", ">")
