"""SCIM content type rendering and parsing.

SCIM uses ``application/scim+json`` per RFC 7644 §3.1.
"""

from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer


class ScimJSONRenderer(JSONRenderer):
    media_type = "application/scim+json"
    format = "scim+json"


class ScimJSONParser(JSONParser):
    media_type = "application/scim+json"


class ScimJSONParserPlain(JSONParser):
    """Accept plain ``application/json`` from SCIM clients.

    The spec recommends ``application/scim+json``, but many production SCIM
    clients (and DRF test helpers) send ``application/json``. Accepting both
    matches reference SCIM servers (Okta, Entra ID).
    """

    media_type = "application/json"
