from factory import django, fuzzy

from waldur_core.permissions import models


class RoleFactory(django.DjangoModelFactory):
    class Meta:
        model = models.Role

    name = fuzzy.FuzzyText()
    description_en = fuzzy.FuzzyText()
    description_et = fuzzy.FuzzyText()
    is_active = True

    @classmethod
    def get_url(cls, role, action=None):
        url = f"/api/roles/{role.uuid}/"
        if action:
            url = f"{url}{action}/"
        return url
