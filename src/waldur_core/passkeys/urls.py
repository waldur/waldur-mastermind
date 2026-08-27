from django.urls import path

from waldur_core.passkeys import ceremony_views, views


def register_in(router):
    router.register(
        r"passkeys",
        views.PasskeyCredentialViewSet,
        basename="passkey",
    )


urlpatterns = [
    path(
        "api/passkeys/registration/begin/",
        ceremony_views.PasskeyRegistrationBeginView.as_view(),
        name="passkey-registration-begin",
    ),
    path(
        "api/passkeys/registration/finish/",
        ceremony_views.PasskeyRegistrationFinishView.as_view(),
        name="passkey-registration-finish",
    ),
    path(
        "api/passkeys/signin/begin/",
        ceremony_views.PasskeySigninBeginView.as_view(),
        name="passkey-signin-begin",
    ),
    path(
        "api/passkeys/signin/finish/",
        ceremony_views.PasskeySigninFinishView.as_view(),
        name="passkey-signin-finish",
    ),
    path(
        "api/passkeys/mfa/begin/",
        ceremony_views.PasskeyMfaBeginView.as_view(),
        name="passkey-mfa-begin",
    ),
    path(
        "api/passkeys/mfa/finish/",
        ceremony_views.PasskeyMfaFinishView.as_view(),
        name="passkey-mfa-finish",
    ),
]
