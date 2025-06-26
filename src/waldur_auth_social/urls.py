from django.urls import path

from . import const, views

urlpatterns = [
    path(
        "api/remote-eduteams/",
        views.RemoteEduteamsView.as_view(),
        name="auth_remote_eduteams",
    ),
]

for provider in const.ProviderChoices.CHOICES:
    urlpatterns.append(
        path(
            f"api-auth/{provider}/init/",
            views.OAuthViewInit.as_view(),
            kwargs={"provider": provider},
            name=f"auth_{provider}_init",
        )
    )
    urlpatterns.append(
        path(
            f"api-auth/{provider}/complete/",
            views.OAuthViewComplete.as_view(),
            kwargs={"provider": provider},
            name=f"auth_{provider}_complete",
        )
    )


def register_in(router):
    router.register(r"identity-providers", views.IdentityProvidersViewSet)
