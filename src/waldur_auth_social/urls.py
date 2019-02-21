from __future__ import unicode_literals

from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^api-auth/google/$', views.GoogleView.as_view(), name='auth_google'),
    url(r'^api-auth/facebook/$', views.FacebookView.as_view(), name='auth_facebook'),
    url(r'^api-auth/smartidee/$', views.SmartIDeeView.as_view(), name='auth_smartidee'),
    url(r'^api-auth/tara/$', views.TARAView.as_view(), name='auth_tara'),
    url(r'^api-auth/registration/$', views.RegistrationView.as_view(), name='auth_registration'),
    url(r'^api-auth/activation/$', views.ActivationView.as_view()),
]
