from django.urls import path

from . import const, views

urlpatterns = [
    path(
        "api/remote-eduteams/",
        views.RemoteEduteamsView.as_view(),
        name="auth_remote_eduteams",
    ),
    path(
        "api/identity-bridge/",
        views.IdentityBridgeView.as_view(),
        name="auth_identity_bridge",
    ),
    path(
        "api/identity-bridge/remove/",
        views.IdentityBridgeRemoveView.as_view(),
        name="auth_identity_bridge_remove",
    ),
    path(
        "api/identity-bridge/allowed-fields/",
        views.IdentityBridgeAllowedFieldsView.as_view(),
        name="auth_identity_bridge_allowed_fields",
    ),
    path(
        "api/identity-bridge/stats/",
        views.IdentityBridgeStatsView.as_view(),
        name="auth_identity_bridge_stats",
    ),
]

urlpatterns.append(
    path(
        "api-auth/default/init/",
        views.OAuthViewDefaultInit.as_view(),
        name="auth_default_init",
    )
)

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
    router.register(
        r"identity-providers",
        views.IdentityProvidersViewSet,
        basename="identity-providers",
    )
