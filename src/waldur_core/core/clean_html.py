import bleach
from bleach.css_sanitizer import CSSSanitizer

ALLOWED_TAGS = [
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
]


ALLOWED_ATTRIBUTES = {
    "a": ["href"],
    "p": ["style"],
    "span": ["style"],
}

css_sanitizer = CSSSanitizer(
    allowed_css_properties=["font-size", "font-weight", "text-align"]
)


def clean_html(value):
    return bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        css_sanitizer=css_sanitizer,
        strip=True,
    )


def unescape_html(value):
    return value.replace("&lt;", "<").replace("&gt;", ">")
