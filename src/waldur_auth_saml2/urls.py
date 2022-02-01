from django.urls import re_path

from .views import (
    Saml2LoginCompleteView,
    Saml2LoginView,
    Saml2LogoutCompleteView,
    Saml2LogoutView,
    Saml2ProviderView,
    metadata,
)

urlpatterns = [
    re_path(
        r'^api-auth/saml2/login/complete/$',
        Saml2LoginCompleteView.as_view(),
        name='saml2-login-complete',
    ),
    re_path(r'^api-auth/saml2/login/$', Saml2LoginView.as_view(), name='saml2-login'),
    re_path(
        r'^api-auth/saml2/logout/complete/$',
        Saml2LogoutCompleteView.as_view(),
        name='saml2-logout-complete',
    ),
    re_path(
        r'^api-auth/saml2/logout/$', Saml2LogoutView.as_view(), name='saml2-logout'
    ),
    re_path(r'^api-auth/saml2/metadata/$', metadata, name='saml2-metadata'),
    re_path(
        r'^api-auth/saml2/providers/$',
        Saml2ProviderView.as_view(),
        name='saml2-provider',
    ),
]
