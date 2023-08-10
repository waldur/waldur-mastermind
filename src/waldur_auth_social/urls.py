from django.urls import path

from . import models, views

urlpatterns = [
    path(
        'api/remote-eduteams/',
        views.RemoteEduteamsView.as_view(),
        name='auth_remote_eduteams',
    ),
]

for provider in models.ProviderChoices.CHOICES:
    urlpatterns.append(
        path(
            f'api-auth/{provider}/',
            views.OAuthView.as_view(),
            kwargs={'provider': provider},
            name=f'auth_{provider}',
        )
    )


def register_in(router):
    router.register(r'identity-providers', views.IdentityProvidersViewSet)
