from rest_framework import test, status

from waldur_core.logging.models import Alert
from waldur_core.logging.tests.factories import AlertFactory
from waldur_core.structure.models import CustomerRole
from waldur_core.structure.tests import factories


class FilterAlertsByAggregateTest(test.APITransactionTestCase):

    def setUp(self):
        self.users = []
        self.customers = []
        self.projects = []

        self.project_alerts = []
        self.customer_alerts = []

        for i in range(2):
            customer = factories.CustomerFactory()
            user = factories.UserFactory()
            customer.add_user(user, CustomerRole.OWNER)

            project = factories.ProjectFactory(customer=customer)
            resource = self.create_resource(customer, project)
            spl = resource.service_project_link
            service = spl.service

            project_alerts = (
                AlertFactory(scope=project),
                AlertFactory(scope=resource),
                AlertFactory(scope=spl)
            )

            customer_alerts = project_alerts + (
                AlertFactory(scope=service),
                AlertFactory(scope=customer)
            )

            self.users.append(user)
            self.customers.append(customer)
            self.projects.append(project)

            self.project_alerts.append(project_alerts)
            self.customer_alerts.append(customer_alerts)

        # Cleanup other alerts
        alert_ids = [alert.pk for alerts in self.customer_alerts for alert in alerts]
        Alert.objects.exclude(pk__in=alert_ids).delete()

    def test_alert_can_be_filtered_by_customer(self):
        for user, customer, alerts in zip(self.users, self.customers, self.customer_alerts):
            query = {
                'aggregate': 'customer',
                'uuid': customer.uuid.hex
            }
            self.check_alerts(user, query, alerts)

    def test_alert_can_be_filtered_by_project(self):
        for user, project, alerts in zip(self.users, self.projects, self.project_alerts):
            self.client.force_authenticate(user)
            query = {
                'aggregate': 'project',
                'uuid': project.uuid.hex
            }
            self.check_alerts(user, query, alerts)

    def check_alerts(self, user, query, alerts):
        self.client.force_authenticate(user)
        response = self.client.get(AlertFactory.get_list_url(), data=query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected = set(alert.uuid.hex for alert in alerts)
        actual = set(alert['uuid'] for alert in response.data)
        self.assertEqual(expected, actual)

    def create_resource(self, customer, project):
        settings = factories.ServiceSettingsFactory(customer=customer)
        service = factories.TestServiceFactory(customer=customer, settings=settings)
        spl = factories.TestServiceProjectLinkFactory(service=service, project=project)
        resource = factories.TestNewInstanceFactory(service_project_link=spl)
        return resource
