from django.contrib.admin.sites import AdminSite
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from reversion.models import Version

from waldur_core.core.admin import UserAdmin
from waldur_core.core.models import User
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.core.utils import make_random_password
from waldur_core.structure.admin import CustomerAdmin
from waldur_core.structure.models import Customer
from waldur_core.structure.tests.factories import CustomerFactory, UserFactory


class MockRequest:
    pass


class MockSuperUser:
    def has_perm(self, perm):
        return True


request = MockRequest()
request.user = MockSuperUser()


class UserAdminTest(TestCase):
    def change_user(self, **kwargs):
        user = UserFactory()
        ma = UserAdmin(User, AdminSite())
        UserChangeForm = ma.get_form(request, user, change=True)
        form_for_data = UserChangeForm(instance=user)

        post_data = form_for_data.initial
        post_data.update(kwargs)

        form = UserChangeForm(instance=user, data=post_data)
        form.save()

        user.refresh_from_db()
        return user

    def test_civil_number_is_stripped(self):
        user = self.change_user(civil_number="  NEW_CIVIL_NUMBER  ")
        self.assertEqual(user.civil_number, "NEW_CIVIL_NUMBER")

    def test_whitespace_civil_number_converts_to_none(self):
        user = self.change_user(civil_number="  ")
        self.assertEqual(user.civil_number, None)

    def test_empty_civil_number_converts_to_none(self):
        user = self.change_user(civil_number="")
        self.assertEqual(user.civil_number, None)


class NativeNameAdminTest(TestCase):
    @override_waldur_core_settings(NATIVE_NAME_ENABLED=False)
    def test_native_name_is_omitted_in_user_admin_if_feature_is_not_enabled(self):
        user = UserFactory()
        ma = UserAdmin(User, AdminSite())
        self.assertFalse("native_name" in ma.get_list_display(request))
        self.assertFalse("native_name" in ma.get_search_fields(request))
        self.assertTrue(
            all(
                "native_name" not in fieldset[1]["fields"]
                for fieldset in ma.get_fieldsets(request, user)
            )
        )

    @override_waldur_core_settings(NATIVE_NAME_ENABLED=True)
    def test_native_name_is_rendered_in_user_admin_if_feature_is_enabled(self):
        user = UserFactory()
        ma = UserAdmin(User, AdminSite())
        self.assertTrue("native_name" in ma.get_list_display(request))
        self.assertTrue("native_name" in ma.get_search_fields(request))
        self.assertTrue(
            any(
                "native_name" in fieldset[1]["fields"]
                for fieldset in ma.get_fieldsets(request, user)
            )
        )

    @override_waldur_core_settings(NATIVE_NAME_ENABLED=False)
    def test_native_name_is_omitted_in_customer_admin_if_feature_is_disabled(self):
        customer = CustomerFactory()
        ma = CustomerAdmin(Customer, AdminSite())
        self.assertFalse("native_name" in ma.get_fields(request, customer))

    @override_waldur_core_settings(NATIVE_NAME_ENABLED=True)
    def test_native_name_is_rendered_in_customer_admin_if_feature_is_enabled(self):
        customer = CustomerFactory()
        ma = CustomerAdmin(Customer, AdminSite())
        self.assertTrue("native_name" in ma.get_fields(request, customer))


class UserReversionTest(TestCase):
    @override_settings(
        AUTHENTICATION_BACKENDS=("django.contrib.auth.backends.ModelBackend",)
    )
    def test_new_revisions_are_not_created_on_each_authentication(self):
        staff = UserFactory(is_staff=True, is_superuser=True)
        staff_password = make_random_password()
        staff.set_password(staff_password)
        staff.save()
        self.assertTrue(
            self.client.login(username=staff.username, password=staff_password)
        )

        url = "/admin/core/user/add/"
        user_password = make_random_password()
        self.client.post(
            url,
            {
                "username": "test",
                "password1": user_password,
                "password2": user_password,
            },
        )
        user = User.objects.get(username="test")
        ct = ContentType.objects.get_for_model(user)
        self.assertEqual(
            Version.objects.filter(object_id=user.id, content_type=ct).count(), 1
        )

        user.is_staff = True
        user.save()
        self.assertTrue(
            self.client.login(username=user.username, password=user_password)
        )
        self.assertEqual(
            Version.objects.filter(object_id=user.id, content_type=ct).count(), 1
        )
