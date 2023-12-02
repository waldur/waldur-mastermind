import yaml
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from waldur_auth_social.models import IdentityProvider
from waldur_auth_social.serializers import IdentityProviderSerializer


class Command(BaseCommand):
    help = """
    Import OIDC auth configuration in YAML format. The example of auth.yaml:

        -  provider: "keycloak"     # OIDC identity provider in string format. Valid values are: "tara", "eduteams", "keycloak".
            label: "Keycloak"       # Human-readable IdP name.
            client_id: "waldur"     # A string used in OIDC requests for client identification.
            client_secret: OIDC_CLIENT_SECRET
            discovery_url: "http://localhost/auth/realms/YOUR_KEYCLOAK_REALM/.well-known/openid-configuration" # OIDC discovery endpoint.
            management_url: ""      # Endpoint for user details management.
            protected_fields:       # User fields that are imported from IdP.
                - "full_name"
                - "email"
    """

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            'auth_file',
            help='Specifies location of auth configuration file.',
        )

    def handle(self, *args, **options):
        with open(options['auth_file']) as auth_file:
            providers = yaml.safe_load(auth_file)
            if providers is None:
                return
            for data in providers:
                try:
                    instance = IdentityProvider.objects.get(provider=data['provider'])
                except IdentityProvider.DoesNotExist:
                    instance = None

                serializer = IdentityProviderSerializer(data=data, instance=instance)
                try:
                    serializer.is_valid(raise_exception=True)
                except ValidationError as e:
                    raise CommandError(e)
                else:
                    serializer.save()
                    if not instance:
                        self.stdout.write(
                            self.style.SUCCESS(
                                'Identity provider %s has been created.'
                                % data['provider']
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.SUCCESS(
                                'Identity provider %s has been updated.'
                                % data['provider']
                            )
                        )
