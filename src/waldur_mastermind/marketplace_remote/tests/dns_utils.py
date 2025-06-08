"""
DNS mocking utilities for marketplace_remote tests.

This module provides DNS mocking functionality that preserves database connectivity
while mocking external DNS resolution for testing purposes.
"""

import socket
from unittest import mock


def create_selective_dns_mock():
    """
    Create a DNS mock that only affects external services,
    preserving internal database service resolution.

    This function creates a mock that intercepts socket.getaddrinfo calls
    and only mocks external hostnames while allowing internal services
    (like database connections) to resolve normally.

    Returns:
        unittest.mock.patch: A mock patch that can be started/stopped
    """
    # Store the original function
    original_getaddrinfo = socket.getaddrinfo

    def mock_getaddrinfo(host, port, *args, **kwargs):
        # Internal services that should NOT be mocked
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

        # For external services, return the mock response
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port or 0)),
        ]

    return mock.patch("socket.getaddrinfo", side_effect=mock_getaddrinfo)


class SelectiveDNSMockMixin:
    """
    Mixin class that provides selective DNS mocking for test classes.

    Usage:
        class MyTestClass(SelectiveDNSMockMixin, test.APITransactionTestCase):
            def setUp(self):
                super().setUp()
                # Your additional setup code here
    """

    def setUp(self):
        """Set up selective DNS mocking."""
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        super().setUp()

    def tearDown(self):
        """Clean up DNS mocking."""
        self.dns_patcher.stop()
        super().tearDown()
