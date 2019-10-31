from django.conf.urls import url

from waldur_mastermind.support import views


def register_in(router):
    router.register(r'support-issues', views.IssueViewSet, basename='support-issue')
    router.register(r'support-priorities', views.PriorityViewSet, basename='support-priority')
    router.register(r'support-comments', views.CommentViewSet, basename='support-comment')
    router.register(r'support-users', views.SupportUserViewSet, basename='support-user')
    router.register(r'support-offerings', views.OfferingViewSet, basename='support-offering')
    router.register(r'support-attachments', views.AttachmentViewSet, basename='support-attachment')
    router.register(r'support-templates', views.TemplateViewSet, basename='support-template')
    router.register(r'support-offering-templates', views.OfferingTemplateViewSet, basename='support-offering-template')
    router.register(r'support-offering-plans', views.OfferingPlanViewSet, basename='support-offering-plan')


urlpatterns = [
    url(r'^api/support-jira-webhook/$', views.WebHookReceiverView.as_view(), name='web-hook-receiver'),
]
