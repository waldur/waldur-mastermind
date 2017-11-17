from __future__ import unicode_literals

from django.conf.urls import url

from waldur_mastermind.support import views


def register_in(router):
    router.register(r'support-issues', views.IssueViewSet, base_name='support-issue')
    router.register(r'support-comments', views.CommentViewSet, base_name='support-comment')
    router.register(r'support-users', views.SupportUserViewSet, base_name='support-user')
    router.register(r'support-offerings', views.OfferingViewSet, base_name='support-offering')

urlpatterns = [
    url(r'^api/support-jira-webhook/$', views.WebHookReceiverView.as_view(), name='web-hook-receiver'),
]
