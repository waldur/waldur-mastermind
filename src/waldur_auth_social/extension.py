from waldur_core.core import WaldurExtension


class AuthSocialExtension(WaldurExtension):
    class Settings:
        # wiki: https://opennode.atlassian.net/wiki/display/WD/AuthSocial+plugin+configuration
        WALDUR_AUTH_SOCIAL = {
            'FACEBOOK_SECRET': '',
            'FACEBOOK_CLIENT_ID': '',
            'SMARTIDEE_SECRET': '',
            'SMARTIDEE_CLIENT_ID': '',
            'TARA_SECRET': '',
            'TARA_CLIENT_ID': '',
            'TARA_SANDBOX': True,
            'TARA_LABEL': 'Riigi Autentimisteenus',
            'KEYCLOAK_LABEL': 'Keycloak',
            # https://www.keycloak.org/docs/latest/securing_apps/#client-id-and-client-secret
            'KEYCLOAK_CLIENT_ID': '',
            'KEYCLOAK_SECRET': '',
            # https://www.keycloak.org/docs/latest/securing_apps/#authorization-endpoint
            'KEYCLOAK_AUTH_URL': '',
            # https://www.keycloak.org/docs/latest/securing_apps/#token-endpoint
            'KEYCLOAK_TOKEN_URL': '',
            'KEYCLOAK_USERINFO_URL': '',
            'EDUTEAMS_LABEL': 'Eduteams',
            'EDUTEAMS_CLIENT_ID': '',
            'EDUTEAMS_SECRET': '',
            'EDUTEAMS_AUTH_URL': 'https://proxy.acc.eduteams.org/saml2sp/OIDC/authorization',
            'EDUTEAMS_TOKEN_URL': 'https://proxy.acc.eduteams.org/OIDC/token',
            'EDUTEAMS_USERINFO_URL': 'https://proxy.acc.eduteams.org/OIDC/userinfo',
            'REMOTE_EDUTEAMS_ACCESS_TOKEN': '',
            'REMOTE_EDUTEAMS_USERINFO_URL': '',
        }

    @staticmethod
    def get_public_settings():
        return [
            'FACEBOOK_CLIENT_ID',
            'SMARTIDEE_CLIENT_ID',
            'TARA_CLIENT_ID',
            'TARA_SANDBOX',
            'TARA_LABEL',
            'KEYCLOAK_CLIENT_ID',
            'KEYCLOAK_LABEL',
            'KEYCLOAK_AUTH_URL',
            'EDUTEAMS_CLIENT_ID',
            'EDUTEAMS_LABEL',
            'EDUTEAMS_AUTH_URL',
        ]

    @staticmethod
    def django_app():
        return 'waldur_auth_social'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns
