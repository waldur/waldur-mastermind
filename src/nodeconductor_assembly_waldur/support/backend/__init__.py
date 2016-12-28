from django.conf import settings


def get_active_backend():
    return globals()[settings.WALDUR_SUPPORT['ACTIVE_BACKEND']]()


class SupportBackendError(Exception):
    pass


class SupportBackend(object):
    """ Interface for support backend """
    def create_issue(self, issue):
        pass

    def update_issue(self, issue):
        pass

    def delete_issue(self, issue):
        pass

    def create_comment(self, comment):
        pass

    def update_comment(self, comment):
        pass

    def delete_comment(self, comment):
        pass

    def get_users(self):
        """
        This method should return all users that are related to support project on backend.

        Each user should be represented as not saved SupportUser instance.
        """
        pass
