from django.conf.urls import url

from . import views


def register_in(router):
    router.register(r'jira', views.JiraServiceViewSet, basename='jira')
    router.register(r'jira-service-project-link', views.JiraServiceProjectLinkViewSet, basename='jira-spl')
    router.register(r'jira-attachments', views.AttachmentViewSet, basename='jira-attachments')
    router.register(r'jira-project-templates', views.ProjectTemplateViewSet, basename='jira-project-templates')
    router.register(r'jira-projects', views.ProjectViewSet, basename='jira-projects')
    router.register(r'jira-issue-types', views.IssueTypeViewSet, basename='jira-issue-types')
    router.register(r'jira-priorities', views.PriorityViewSet, basename='jira-priorities')
    router.register(r'jira-issues', views.IssueViewSet, basename='jira-issues')
    router.register(r'jira-comments', views.CommentViewSet, basename='jira-comments')


urlpatterns = [
    url(r'^api/jira-webhook-receiver/$', views.WebHookReceiverViewSet.as_view(), name='jira-web-hook'),
]
