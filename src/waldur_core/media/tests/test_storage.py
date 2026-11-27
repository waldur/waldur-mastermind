from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from waldur_core.media import models
from waldur_core.media.storage import DatabaseStorage


class DatabaseStorageMetadataTest(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()
        self.name = self.storage.save("docs/report.pdf", ContentFile(b"%PDF-1.4 x"))
        self.file = models.File.objects.get(name=self.name)

    def test_url_and_size_of_a_stored_file(self):
        self.assertEqual(
            self.storage.url(self.name),
            reverse("media", kwargs={"uuid": self.file.uuid.hex}),
        )
        self.assertEqual(self.storage.size(self.name), self.file.size)

    def test_url_and_size_of_a_missing_file(self):
        self.assertIsNone(self.storage.url("docs/missing.pdf"))
        self.assertEqual(self.storage.size("docs/missing.pdf"), 0)

    def test_url_and_size_do_not_read_the_content(self):
        with CaptureQueriesContext(connection) as context:
            self.storage.url(self.name)
            self.storage.size(self.name)

        self.assertEqual(len(context.captured_queries), 2)
        for query in context.captured_queries:
            self.assertNotIn('"media_file"."content"', query["sql"])
