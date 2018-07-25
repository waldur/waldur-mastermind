from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


User = get_user_model()


class TestAdminEndpoints(TestCase):

    def setUp(self):
        user, _ = User.objects.get_or_create(username='username', is_staff=True)
        self.client.force_login(user)
        self.admin_site_name = admin.site.name

    def _reverse_url(self, path):
        return reverse('%s:%s' % (self.admin_site_name, path))

    def test_app_list_ulrs_can_be_queried(self):
        app_list_urls = dict()
        for model in admin.site._registry:
            app_list_url = reverse('%s:%s' % (self.admin_site_name, 'app_list'), args=(model._meta.app_label,))
            app_list_urls.update({model._meta.app_label: app_list_url})

        for url in app_list_urls.values():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_base_admin_site_urls_can_be_queried(self):
        pages = ['index', 'login', 'logout', 'password_change', 'password_change_done', 'jsi18n']
        for name in pages:
            url = self._reverse_url(name)
            response = self.client.get(url)
            self.assertIn(response.status_code, [200, 302])

    def test_changelist_urls_can_be_queried(self):
        for model in admin.site._registry:
            url = self._reverse_url('%s_%s_changelist' % (model._meta.app_label, model._meta.model_name))
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_add_urls_can_be_queried(self):
        for model in admin.site._registry:
            model_fullname = '%s_%s' % (model._meta.app_label, model._meta.model_name)
            url = self._reverse_url('%s_add' % model_fullname)
            response = self.client.get(url)
            self.assertIn(response.status_code, [200, 403])
