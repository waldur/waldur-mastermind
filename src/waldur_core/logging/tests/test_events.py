import unittest

from django.conf import settings
from django.test import override_settings
from rest_framework import status
from rest_framework import test
from six.moves import mock

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .. import utils
from ..loggers import EventLogger, event_logger


def override_elasticsearch_settings(**kwargs):
    waldur_settings = settings.WALDUR_CORE.copy()
    waldur_settings['ELASTICSEARCH'] = {
        'username': 'username',
        'password': 'password',
        'host': 'example.com',
        'port': '9999',
        'protocol': 'https',
    }
    return override_settings(WALDUR_CORE=waldur_settings, **kwargs)


@override_elasticsearch_settings()
class BaseEventsApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.es_patcher = mock.patch('waldur_core.logging.elasticsearch_client.Elasticsearch')
        self.mocked_es = self.es_patcher.start()
        self.mocked_es().search.return_value = {'hits': {'total': 0, 'hits': []}}
        self.mocked_es().count.return_value = {'count': 0}

    def tearDown(self):
        self.es_patcher.stop()

    def get_term(self, name):
        call_args = self.mocked_es().search.call_args[-1]
        query = call_args['body']['query']['bool']
        if name in query:
            return query[name][-1]['terms']

    @property
    def must_terms(self):
        return self.get_term('must')

    @property
    def must_not_terms(self):
        return self.get_term('must_not')


class DebugEventLogger(EventLogger):

    class Meta:
        event_types = (
            'debug_started',
            'debug_succeeded',
            'debug_failed',
        )
        event_groups = {
            'debug_only': event_types,
        }


class ExtraEventLogger(EventLogger):

    class Meta:
        event_types = (
            'update_started',
            'update_succeeded',
            'update_failed',
        )
        event_groups = {
            'update': event_types
        }


class UserEventLogger(EventLogger):

    class Meta:
        event_types = (
            'user_created',
            'user_deleted',
        )
        event_groups = {
            'user': event_types
        }


class EventGetTest(BaseEventsApiTest):

    def setUp(self):
        super(EventGetTest, self).setUp()
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        self.old_loggers = event_logger.__dict__.copy()
        event_logger.unregister_all()
        event_logger.register('debug_logger', DebugEventLogger)
        event_logger.register('extra_logger', ExtraEventLogger)
        event_logger.register('user_logger', UserEventLogger)

    def tearDown(self):
        event_logger.__dict__ = self.old_loggers

    @override_elasticsearch_settings(DEBUG=True)
    def test_debug_events_are_not_filtered_out_in_debug_mode(self):
        self.get_events()
        self.assertIsNone(self.must_not_terms)

    @override_elasticsearch_settings(DEBUG=False)
    def test_debug_events_are_filtered_out_in_production_mode(self):
        self.get_events()

        self.assertEqual(set(self.must_not_terms['event_type']), {
            'debug_started',
            'debug_succeeded',
            'debug_failed',
        })

    @override_elasticsearch_settings(DEBUG=False)
    def test_extra_and_debug_events_combined(self):
        self.get_events({'exclude_extra': True})
        self.assertEqual(set(self.must_not_terms['event_type']), {
            'debug_started',
            'debug_succeeded',
            'debug_failed',
            'update_started',
            'update_succeeded',
            'update_failed',
        })

    @override_elasticsearch_settings(DEBUG=False)
    def test_features_and_debug_events_combined(self):
        self.get_events({'exclude_features': ['user']})
        self.assertEqual(set(self.must_not_terms['event_type']), {
            'debug_started',
            'debug_succeeded',
            'debug_failed',
            'user_created',
            'user_deleted',
        })

    def get_events(self, params=None):
        return self.client.get(factories.EventFactory.get_list_url(), params)


class ScopeTypeTest(BaseEventsApiTest):
    def _get_events_by_scope_type(self, model):
        url = factories.EventFactory.get_list_url()
        scope_type = utils.get_reverse_scope_types_mapping()[model]
        return self.client.get(url, {'scope_type': scope_type})

    def test_staff_can_see_any_customers_events(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        customer = structure_factories.CustomerFactory()

        self._get_events_by_scope_type(structure_models.Customer)
        self.assertEqual(self.must_terms, {'customer_uuid': [customer.uuid.hex]})

    def test_owner_can_see_only_customer_events(self):
        structure_factories.CustomerFactory()

        customer = structure_factories.CustomerFactory()
        owner = structure_factories.UserFactory()
        customer.add_user(owner, structure_models.CustomerRole.OWNER)

        self.client.force_authenticate(user=owner)
        self._get_events_by_scope_type(structure_models.Customer)
        self.assertEqual(self.must_terms, {'customer_uuid': [customer.uuid.hex]})

    def test_project_administrator_can_see_his_project_events(self):
        project = structure_factories.ProjectFactory()
        admin = structure_factories.UserFactory()
        project.add_user(admin, structure_models.ProjectRole.ADMINISTRATOR)

        self.client.force_authenticate(user=admin)
        self._get_events_by_scope_type(structure_models.Project)
        self.assertEqual(self.must_terms, {'project_uuid': [project.uuid.hex]})

    def test_project_administrator_cannot_see_other_projects_events(self):
        user = structure_factories.UserFactory()

        structure_factories.ProjectFactory()

        self.client.force_authenticate(user=user)
        self._get_events_by_scope_type(structure_models.Project)
        self.assertEqual(self.must_terms, {'project_uuid': []})

    def test_project_administrator_cannot_see_related_customer_events(self):
        project = structure_factories.ProjectFactory()
        admin = structure_factories.UserFactory()
        project.add_user(admin, structure_models.ProjectRole.ADMINISTRATOR)

        self.client.force_authenticate(user=admin)
        self._get_events_by_scope_type(structure_models.Customer)
        self.assertEqual(self.must_terms, {'customer_uuid': []})


class ScopeTest(BaseEventsApiTest):
    def _get_events_by_scope(self, scope):
        url = factories.EventFactory.get_list_url()
        return self.client.get(url, {'scope': scope})

    @unittest.skip('NC-1485')
    def test_project_administrator_cannot_see_related_customer_events(self):
        project = structure_factories.ProjectFactory()
        admin = structure_factories.UserFactory()
        project.add_user(admin, structure_models.ProjectRole.ADMINISTRATOR)

        self.client.force_authenticate(user=admin)
        response = self._get_events_by_scope(structure_factories.CustomerFactory.get_url(project.customer))
        self.assertFalse(self.mocked_es().search.called)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_owner_can_see_his_customer_events(self):
        customer = structure_factories.CustomerFactory()
        owner = structure_factories.UserFactory()
        customer.add_user(owner, structure_models.CustomerRole.OWNER)

        self.client.force_authenticate(user=owner)
        self._get_events_by_scope(structure_factories.CustomerFactory.get_url(customer))
        self.assertEqual(self.must_terms, {'customer_uuid.keyword': [customer.uuid.hex]})
