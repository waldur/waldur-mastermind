from django.test import TestCase

from waldur_core.core import features
from waldur_core.core.models import Feature
from waldur_core.core.views import get_feature_values


class FeatureDefaultsTest(TestCase):
    def test_default_on_feature_is_enabled_without_a_row(self):
        self.assertTrue(features.is_enabled("deployment.send_metrics"))
        self.assertTrue(get_feature_values()["deployment"]["send_metrics"])

    def test_explicit_value_overrides_default(self):
        Feature.objects.create(key="deployment.send_metrics", value=False)

        self.assertFalse(features.is_enabled("deployment.send_metrics"))
        self.assertFalse(get_feature_values()["deployment"]["send_metrics"])

    def test_other_features_default_to_off(self):
        self.assertFalse(features.is_enabled("deployment.enable_cookie_notice"))
        self.assertFalse(get_feature_values()["deployment"]["enable_cookie_notice"])
