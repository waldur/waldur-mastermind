import yaml
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from waldur_auth_social.models import IdentityProvider
from waldur_auth_social.serializers import IdentityProviderSerializer


class Command(BaseCommand):
    help = """Import OIDC auth configuration in YAML format. The example of auth.yaml:
        -  provider: "keycloak"     # OIDC identity provider in string format. Valid values are: "tara","eduteams","keycloak".
            label: "Keycloak"       # Human-readable identity provider name
            client_id: "waldur"     # String used in OIDC requests and keycloak database for client identification.
            client_secret: K3YCLOAK_SCRT   # Keycloak secret code for client configuration.
            discovery_url: "http://localhost/auth/realms/YOUR_KEYCLOAK_REALM/.well-known/openid-configuration" # Keycloak OIDC discovery endpoint.
            management_url: ""      # The endpoint for user details management.
            protected_fields:       # Validated json fields
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
