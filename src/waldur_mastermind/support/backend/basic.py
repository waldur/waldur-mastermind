from . import SupportBackend


class BasicBackend(SupportBackend):
    backend_name = "basic"

    def create_issue(self, issue):
        return

    def update_issue(self, issue):
        return

    def delete_issue(self, issue):
        return

    def create_comment(self, comment):
        return

    def update_comment(self, comment):
        return

    def delete_comment(self, comment):
        return

    def create_attachment(self, attachment):
        return

    def delete_attachment(self, attachment):
        return

    def get_users(self):
        return

    def pull_priorities(self):
        return

    def create_issue_links(self, *args, **kwargs):
        return

    def get_issue_details(self):
        return {}

    def periodic_task(self):
        return
