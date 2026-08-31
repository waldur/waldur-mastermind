import tempfile

from django.core.cache import cache
from django.core.management import call_command
from django.template import TemplateDoesNotExist, engines
from django.test import TestCase

from waldur_core.core import db_template_cache
from waldur_core.core.template_loaders import DatabaseTemplateLoader
from waldur_core.structure.tests.factories import NotificationTemplateFactory


class DatabaseTemplateLoaderTest(TestCase):
    def setUp(self):
        self.loader = DatabaseTemplateLoader(engines["django"].engine)
        cache.clear()

    def _origin(self, path):
        return next(self.loader.get_template_sources(path))

    def test_cache_hit_is_served_without_a_query(self):
        cache.set(db_template_cache.get_cache_key("some/path.txt"), "cached content")

        with self.assertNumQueries(0):
            content = self.loader.get_contents(self._origin("some/path.txt"))

        self.assertEqual(content, "cached content")

    def test_notfound_sentinel_raises_without_a_query(self):
        cache.set(db_template_cache.get_cache_notfound_key("some/path.txt"), True)

        with self.assertNumQueries(0):
            with self.assertRaises(TemplateDoesNotExist):
                self.loader.get_contents(self._origin("some/path.txt"))

    def test_db_hit_warms_the_cache(self):
        template = NotificationTemplateFactory(
            path="some/path.txt", content="db content"
        )
        # The post_save signal already cached this; clear it to isolate the
        # loader's own warming behavior from the signal's.
        cache.delete(db_template_cache.get_cache_key(template.path))

        content = self.loader.get_contents(self._origin("some/path.txt"))

        self.assertEqual(content, "db content")
        self.assertEqual(
            cache.get(db_template_cache.get_cache_key("some/path.txt")), "db content"
        )

    def test_blank_content_plants_sentinel_and_falls_through(self):
        NotificationTemplateFactory(path="some/path.txt", content="")
        # The post_save signal already planted the sentinel on creation; clear
        # it to isolate the loader's own blank-content handling from the
        # signal's, matching test_db_hit_warms_the_cache's approach.
        cache.delete(db_template_cache.get_cache_notfound_key("some/path.txt"))

        with self.assertRaises(TemplateDoesNotExist):
            self.loader.get_contents(self._origin("some/path.txt"))

        self.assertTrue(
            cache.get(db_template_cache.get_cache_notfound_key("some/path.txt"))
        )


class OverrideTemplatesCleanRenderTest(TestCase):
    """Integration-level: --clean should make the next render return the
    filesystem template, not the override, and not an empty string (regression
    test for the blank-content cache-poisoning bug found during this MR)."""

    def setUp(self):
        cache.clear()

    def test_clean_falls_back_to_filesystem_content_on_next_render(self):
        path = "users/invitation_created_subject.txt"
        template = NotificationTemplateFactory(path=path, content="overridden subject")

        rendered_while_overridden = engines["django"].get_template(path).render({})
        self.assertEqual(rendered_while_overridden, "overridden subject")

        call_command(
            "override_templates",
            self._empty_overrides_file(),
            clean=True,
        )

        template.refresh_from_db()
        self.assertEqual(template.content, "")

        rendered_after_clean = engines["django"].get_template(path).render({})
        self.assertNotEqual(rendered_after_clean, "")
        self.assertNotEqual(rendered_after_clean, "overridden subject")

    def _empty_overrides_file(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write("{}\n")
        f.close()
        return f.name
