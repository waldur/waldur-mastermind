import uuid

import orjson
from django.test import TestCase

from waldur_core.core.renderers import WaldurORJSONRenderer


class WaldurORJSONRendererTest(TestCase):
    def setUp(self):
        self.renderer = WaldurORJSONRenderer()

    def test_integer_keyed_dict_is_rendered(self):
        # DRF ListField validation errors are keyed by the integer item index.
        # The renderer must not raise "Dict key must be str" on such payloads.
        data = {0: ["Not a valid string."], 1: ["Not a valid string."]}

        content = self.renderer.render(data)

        self.assertEqual(orjson.loads(content), {"0": data[0], "1": data[1]})

    def test_none_is_rendered_as_empty_bytes(self):
        self.assertEqual(self.renderer.render(None), b"")

    def test_string_uuid_is_rendered(self):
        value = uuid.uuid4()
        content = self.renderer.render({"uuid": value})

        self.assertEqual(orjson.loads(content), {"uuid": str(value)})
