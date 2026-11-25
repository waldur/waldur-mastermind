"""HEAD responses carry the headers of the matching GET and no body.

Django renders a HEAD request through the GET view and keeps the body; only
its test client drops it, so these tests drive the WSGI handler itself, the way
gunicorn does, and gunicorn logs a warning for every body it has to discard.
"""

import io
from contextlib import contextmanager
from wsgiref.util import setup_testing_defaults

from django.core.handlers.wsgi import WSGIHandler
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from waldur_core.server.middleware import head_response_body_removal


@contextmanager
def connections_left_open():
    """Detach close_old_connections, as Django's test client does.

    Starting or finishing a request, which closing a response does, would
    otherwise check the test's database connection: it closes the one a test
    case's transaction runs on, and it is refused where no database is allowed.
    """
    request_started.disconnect(close_old_connections)
    request_finished.disconnect(close_old_connections)
    try:
        yield
    finally:
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)


def call_wsgi(method, path):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "testserver"}
    setup_testing_defaults(environ)
    started = {}

    def start_response(status, headers, exc_info=None):
        started["status"] = status
        started["headers"] = dict(headers)

    with connections_left_open():
        result = WSGIHandler()(environ, start_response)
        try:
            body = b"".join(result)
        finally:
            result.close()
    return started["status"], started["headers"], body


class HeadThroughWsgiTest(TestCase):
    def test_head_response_has_no_body(self):
        status, headers, body = call_wsgi("HEAD", "/api/customers/")

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(body, b"")

    def test_head_keeps_the_get_content_length(self):
        _, get_headers, get_body = call_wsgi("GET", "/api/customers/")
        _, head_headers, _ = call_wsgi("HEAD", "/api/customers/")

        self.assertTrue(get_body)
        self.assertEqual(head_headers["Content-Length"], str(len(get_body)))
        self.assertEqual(head_headers["Content-Type"], get_headers["Content-Type"])

    def test_get_response_keeps_its_body(self):
        _, _, body = call_wsgi("GET", "/api/customers/")

        self.assertIn(b"detail", body)


class HeadMiddlewareTest(SimpleTestCase):
    def run_middleware(self, method, response):
        request = getattr(RequestFactory(), method.lower())("/")
        return head_response_body_removal(lambda _: response)(request)

    def test_plain_response_body_is_dropped(self):
        response = self.run_middleware("HEAD", HttpResponse(b"[]"))

        self.assertEqual(response.content, b"")

    def test_streaming_response_body_is_dropped(self):
        response = self.run_middleware(
            "HEAD", StreamingHttpResponse(iter([b"a", b"b"]))
        )

        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_file_response_is_not_handed_to_the_file_wrapper(self):
        stream = io.BytesIO(b"file contents")
        response = self.run_middleware("HEAD", FileResponse(stream))

        self.assertIsNone(response.file_to_stream)
        self.assertEqual(b"".join(response.streaming_content), b"")
        self.assertEqual(response["Content-Length"], str(len(b"file contents")))
        with connections_left_open():
            response.close()
        self.assertTrue(stream.closed)

    def test_other_methods_are_left_alone(self):
        response = self.run_middleware("GET", HttpResponse(b"[]"))

        self.assertEqual(response.content, b"[]")
