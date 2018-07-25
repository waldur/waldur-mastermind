from django.conf.urls import url
from django.views.decorators.csrf import csrf_exempt
from django_openid_auth import views as auth_view

from waldur_core.core.views import validate_authentication_method

from . import views

validate_openid = validate_authentication_method('ESTONIAN_ID')
login_begin = validate_openid(csrf_exempt(auth_view.login_begin))
login_complete = validate_openid(auth_view.login_complete)


urlpatterns = [
    url(r'^api-auth/openid/login/$', login_begin, name='openid-login'),
    url(r'^api-auth/openid/complete/$', login_complete, name='openid-complete'),
    url(r'^api-auth/openid/logo.gif$', auth_view.logo, name='openid-logo'),
    url(r'^api-auth/openid/login_completed/', views.login_completed),
]
