from django.contrib.admin.sites import AdminSite
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from reversion.models import Version

from waldur_core.core.admin import UserAdmin
from waldur_core.core.models import User
from waldur_core.core.utils import make_random_password
from waldur_core.structure.tests.factories import UserFactory


class MockRequest:
    pass


class MockSuperUser:
    is_authenticated = True
    is_staff = True
    is_anonymous = False

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

        # Granting staff is an audited change, so it does add a revision.
        user.is_staff = True
        user.save()
        self.assertEqual(
            Version.objects.filter(object_id=user.id, content_type=ct).count(), 2
        )

        # Authenticating only touches last_login, which must never open one.
        self.assertTrue(
            self.client.login(username=user.username, password=user_password)
        )
        self.assertEqual(
            Version.objects.filter(object_id=user.id, content_type=ct).count(), 2
        )
