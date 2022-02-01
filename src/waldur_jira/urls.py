from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r'jira-attachments', views.AttachmentViewSet, basename='jira-attachments'
    )
    router.register(
        r'jira-project-templates',
        views.ProjectTemplateViewSet,
        basename='jira-project-templates',
    )
    router.register(r'jira-projects', views.ProjectViewSet, basename='jira-projects')
    router.register(
        r'jira-issue-types', views.IssueTypeViewSet, basename='jira-issue-types'
    )
    router.register(
        r'jira-priorities', views.PriorityViewSet, basename='jira-priorities'
    )
    router.register(r'jira-issues', views.IssueViewSet, basename='jira-issues')
    router.register(r'jira-comments', views.CommentViewSet, basename='jira-comments')


urlpatterns = [
    re_path(
        r'^api/jira-webhook-receiver/$',
        views.WebHookReceiverViewSet.as_view(),
        name='jira-web-hook',
    ),
]
