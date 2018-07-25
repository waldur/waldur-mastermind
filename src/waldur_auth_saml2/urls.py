from django.conf.urls import url
from djangosaml2.views import metadata

from .views import Saml2LoginView, Saml2LoginCompleteView, Saml2LogoutView, Saml2LogoutCompleteView, Saml2ProviderView


urlpatterns = [
    url(r'^api-auth/saml2/login/complete/$', Saml2LoginCompleteView.as_view(), name='saml2-login-complete'),
    url(r'^api-auth/saml2/login/$', Saml2LoginView.as_view(), name='saml2-login'),
    url(r'^api-auth/saml2/logout/complete/$', Saml2LogoutCompleteView.as_view(), name='saml2-logout-complete'),
    url(r'^api-auth/saml2/logout/$', Saml2LogoutView.as_view(), name='saml2-logout'),
    url(r'^api-auth/saml2/metadata/$', metadata, name='saml2-metadata'),
    url(r'^api-auth/saml2/providers/$', Saml2ProviderView.as_view(), name='saml2-provider'),
]
