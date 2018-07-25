from django.test import TestCase

from .. import factories as structure_factories


class TagCacheTask(TestCase):
    def test_cache_populated_when_tag_added(self):
        settings = structure_factories.ServiceSettingsFactory()
        settings.tags.add('IAAS')
        self.assertEqual(settings.get_tags(), ['IAAS'])

    def test_cache_cleaned_when_tag_removed(self):
        settings = structure_factories.ServiceSettingsFactory()
        settings.tags.add('IAAS')
        settings.tags.remove('IAAS')
        self.assertEqual(settings.get_tags(), [])
