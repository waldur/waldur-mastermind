import factory
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import models


class GoogleCredentialsFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.GoogleCredentials

    service_provider = factory.SubFactory(marketplace_factories.ServiceProviderFactory)
    client_id = factory.Sequence(lambda n: 'client_id-%s' % n)
    project_id = factory.Sequence(lambda n: 'project_id-%s' % n)
    client_secret = factory.Sequence(lambda n: 'client_secret-%s' % n)

    @classmethod
    def get_url(cls, credentials=None):
        if credentials is None:
            credentials = GoogleCredentialsFactory()

        return (
            'http://testserver'
            + reverse(
                'google_credential-detail',
                kwargs={'uuid': credentials.service_provider.uuid.hex},
            )
            + 'google_credentials/'
        )

    @classmethod
    def get_authorize_url(cls, credentials=None):
        if credentials is None:
            credentials = GoogleCredentialsFactory()
        return 'http://testserver' + reverse(
            'google-auth-detail',
            kwargs={'uuid': credentials.service_provider.uuid.hex},
        )
