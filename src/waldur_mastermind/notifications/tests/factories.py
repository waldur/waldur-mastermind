import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class BroadcastMessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.BroadcastMessage

    author = factory.SubFactory(structure_factories.UserFactory)
    subject = factory.Sequence(lambda n: "subject-%s" % n)
    body = factory.Sequence(lambda n: "body-%s" % n)

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("broadcastmessage-list")
        return url if action is None else url + action + "/"


class AdminAnnouncementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.AdminAnnouncement

    description = factory.Sequence(lambda n: "description-%s" % n)
    type = models.AdminAnnouncement.Type.INFORMATION
    active_from = "2025-01-01T00:00:00Z"
    active_to = "2026-01-02T00:00:00Z"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("admin-announcement-list")
        return url if action is None else url + action + "/"

    @classmethod
    def get_url(cls, instance):
        return "http://testserver" + reverse(
            "admin-announcement-detail", kwargs={"uuid": instance.uuid}
        )
