# from jira import JIRA

# from django.conf import settings


# class SupportBackend(object):
#     """ Interface for support backend """
#     def create_issue(self, issue):
#         pass

#     def update_issue(self, issue):
#         pass

#     def delete_issue(self, issue):
#         pass


# class JIRABackend(SupportBackend):
#     credentials = settings.WALDUR_SUPPORT.get('CREADENTIALS', {})

#     @property
#     def manager(self):
#         if not hasattr(self, '_manager'):
#             self._manager = JIRA(
#                 server=self.credentials['server'],
#                 options={'verify': self.credentials['verify_ssl']},
#                 basic_auth=(self.credentials['username'], self.credentials['password']),
#                 validate=False)
#         return self._manager

#     def create_issue(self, issue):
#         pass
