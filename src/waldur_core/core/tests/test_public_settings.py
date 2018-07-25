from django.test import TestCase
from six.moves import mock

from waldur_core.core import views


class TestPublicSettings(TestCase):

    def setUp(self):
        super(TestPublicSettings, self).setUp()

        class MockExtension(object):
            def __init__(self, name):
                class Settings(object):
                    def __init__(self, name):
                        setattr(self, name, {})

                self.Settings = Settings(name)

            @staticmethod
            def get_public_settings():
                return ['INFO']

        extensions = {
            'WALDUR_EXTENSION_1': {'ENABLED': False},
            'WALDUR_EXTENSION_2': {'ENABLED': True},
            'WALDUR_EXTENSION_3': {'SECRET': 'secret', 'INFO': 'info'}
        }
        mock_settings = mock.Mock(WALDUR_CORE={}, WALDUR_CORE_PUBLIC_SETTINGS=[], **extensions)
        self.patcher_settings = mock.patch('waldur_core.core.views.settings', new=mock_settings)
        self.patcher_settings.start()

        self.patcher = mock.patch('waldur_core.core.views.WaldurExtension')
        self.mock = self.patcher.start()
        self.mock.get_extensions.return_value = [MockExtension(e) for e in extensions.keys()]

    def tearDown(self):
        super(TestPublicSettings, self).tearDown()
        mock.patch.stopall()

    def test_if_extension_not_have_field_enabled_or_it_equally_true_this_extension_must_by_in_response(self):
        response = views.get_public_settings()
        self.assertTrue('WALDUR_EXTENSION_2' in response.keys())
        self.assertTrue('WALDUR_EXTENSION_3' in response.keys())

    def test_if_extension_have_field_enabled_and_it_equally_false_this_extension_not_to_be_in_response(self):
        response = views.get_public_settings()
        self.assertFalse('WALDUR_EXTENSION_1' in response.keys())

    def test_if_field_in_get_public_settings_it_value_must_by_in_response(self):
        response = views.get_public_settings()
        self.assertTrue('INFO' in response['WALDUR_EXTENSION_3'])

    def test_if_field_not_in_get_public_settings_it_value_not_to_be_in_response(self):
        response = views.get_public_settings()
        self.assertFalse('SECRET' in response['WALDUR_EXTENSION_3'])
