import ipaddress
import logging
import socket
from urllib.parse import urlparse

from django.conf import settings

# The waldur_api_client SDK pulls in a large generated attrs/pydantic model graph
# (~70 MB resident). Import it lazily inside get_waldur_client so it only loads in
# processes that actually build a remote client, not at Django startup. See the
# "Lazy imports for heavy optional backends" section of CLAUDE.md.

logger = logging.getLogger(__name__)

BLOCKED_RANGES = [
    ipaddress.ip_network(range) for range in settings.WALDUR_CORE["SUBNET_BLACKLIST"]
]


class ClientValidationError(Exception):
    def __init__(self, message):
        super().__init__(message)
        logger.error(f"Unable to initialize remote client: {message}")


def check_url(url):
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ClientValidationError("Error resolving client hostname")
    if not parsed.scheme:
        raise ClientValidationError("Error resolving url scheme")
    try:
        # The function returns a list of 5-tuples with the following structure:
        # (family, type, proto, canonname, sockaddr)
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ClientValidationError("Error resolving client hostname")
    for addr in addrinfo:
        ip = ipaddress.ip_address(addr[4][0])
        if any(ip in range for range in BLOCKED_RANGES):
            raise ClientValidationError("Access to internal IPs is not allowed")


def get_waldur_client(api_url, token):
    from waldur_api_client.client import AuthenticatedClient

    check_url(api_url)
    return AuthenticatedClient(base_url=api_url.rstrip("/api"), token=token)
