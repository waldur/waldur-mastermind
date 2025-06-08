"""
DNS mocking utilities for SSRF protection tests.

This module provides DNS mocking functionality that preserves database connectivity
while allowing specific DNS resolution responses for SSRF testing.
"""

import socket
from unittest import mock


def create_ssrf_selective_dns_mock(test_hostname_mapping=None):
    """
    Create a DNS mock for SSRF tests that preserves database connectivity
    while allowing specific hostname->IP mappings for testing.

    Args:
        test_hostname_mapping (dict): Optional mapping of hostname -> IP for tests

    Returns:
        unittest.mock.patch: A mock patch that can be started/stopped
    """
    # Store the original function
    original_getaddrinfo = socket.getaddrinfo

    def mock_getaddrinfo(host, port, *args, **kwargs):
        # Internal services that should NOT be mocked (for database connectivity)
        internal_hosts = {
            "postgres",
            "localhost",
            "127.0.0.1",
            "database",
            "db",
            "::1",  # IPv6 localhost
        }

        # If this is an internal service, use real DNS resolution
        if host in internal_hosts:
            return original_getaddrinfo(host, port, *args, **kwargs)

        # If we have a specific mapping for this test, use it
        if test_hostname_mapping and host in test_hostname_mapping:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    (test_hostname_mapping[host], port or 0),
                ),
            ]

        # Default behavior for unmapped external services
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port or 0)),
        ]

    return mock.patch("socket.getaddrinfo", side_effect=mock_getaddrinfo)


def create_ssrf_dns_mock_with_return_value(hostname, ip_address):
    """
    Create a DNS mock that returns a specific IP for a hostname while preserving
    database connectivity.

    Args:
        hostname (str): The hostname to mock
        ip_address (str): The IP address to return for the hostname

    Returns:
        unittest.mock.patch: A mock patch that can be started/stopped
    """
    return create_ssrf_selective_dns_mock({hostname: ip_address})


def create_ssrf_dns_mock_with_error():
    """
    Create a DNS mock that raises gaierror for external hosts while preserving
    database connectivity.

    Returns:
        unittest.mock.patch: A mock patch that can be started/stopped
    """
    # Store the original function
    original_getaddrinfo = socket.getaddrinfo

    def mock_getaddrinfo(host, port, *args, **kwargs):
        # Internal services that should NOT be mocked (for database connectivity)
        internal_hosts = {
            "postgres",
            "localhost",
            "127.0.0.1",
            "database",
            "db",
            "::1",  # IPv6 localhost
        }

        # If this is an internal service, use real DNS resolution
        if host in internal_hosts:
            return original_getaddrinfo(host, port, *args, **kwargs)

        # For external services, raise gaierror
        raise socket.gaierror("Name or service not known")

    return mock.patch("socket.getaddrinfo", side_effect=mock_getaddrinfo)
