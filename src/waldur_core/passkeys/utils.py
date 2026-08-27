import base64


def bytes_to_base64url(value: bytes) -> str:
    """Encode bytes as unpadded base64url, the form WebAuthn uses on the wire."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def base64url_to_bytes(value: str) -> bytes:
    """Decode unpadded base64url.

    The browser omits padding; ``base64`` insists on it, so it is restored
    here rather than at every call site.
    """
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
