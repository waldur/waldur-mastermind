from django.conf.urls import url

from . import views


def register_in(router):
    router.register(r'jira', views.JiraServiceViewSet, base_name='jira')
    router.register(r'jira-service-project-link', views.JiraServiceProjectLinkViewSet, base_name='jira-spl')
    router.register(r'jira-attachments', views.AttachmentViewSet, base_name='jira-attachments')
    router.register(r'jira-project-templates', views.ProjectTemplateViewSet, base_name='jira-project-templates')
    router.register(r'jira-projects', views.ProjectViewSet, base_name='jira-projects')
    router.register(r'jira-issue-types', views.IssueTypeViewSet, base_name='jira-issue-types')
    router.register(r'jira-priorities', views.PriorityViewSet, base_name='jira-priorities')
    router.register(r'jira-issues', views.IssueViewSet, base_name='jira-issues')
    router.register(r'jira-comments', views.CommentViewSet, base_name='jira-comments')


urlpatterns = [
    url(r'^api/jira-webhook-receiver/$', views.WebHookReceiverViewSet.as_view(), name='jira-web-hook'),
]
