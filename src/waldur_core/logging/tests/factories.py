import factory
from django.contrib.contenttypes import models as ct_models
from django.urls import reverse

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.logging import models
from waldur_core.logging.event_logger import get_valid_events
from waldur_core.structure.tests import factories as structure_factories


class EventConsumerFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.EventConsumer],
):
    class Meta:
        model = models.EventConsumer

    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def with_scopes(cls, *instances, user=None, **kwargs):
        """Create an EventConsumer bound to the given entities.

        No instances = a global (unrestricted) consumer.
        """
        params = dict(kwargs)
        if user is not None:
            params["user"] = user
        consumer = cls.create(**params)
        for instance in instances:
            models.EventConsumerScope.objects.create(
                consumer=consumer,
                content_type=ct_models.ContentType.objects.get_for_model(
                    instance.__class__
                ),
                object_id=instance.id,
            )
        return consumer

    @classmethod
    def for_offering(cls, offering, user=None, **kwargs):
        """Create an EventConsumer bound to a single offering."""
        return cls.with_scopes(offering, user=user, **kwargs)


class EventFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Event]
):
    class Meta:
        model = models.Event

    message = factory.Sequence(lambda i: "message#%s" % i)
    event_type = factory.Iterator(
        [
            "first_event",
            "second_event",
            "third_event",
            "fourth_event",
        ]
    )
    context = {
        "customer_abbreviation": "TCAN",
        "customer_contact_details": "test details",
        "customer_name": "Test customer",
        "customer_uuid": "test_customer_uuid",
        "host": "example.com",
        "project_name": "test_project",
        "project_uuid": "test_project_uuid",
        "user_uuid": "test_user_uuid",
    }

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("event-list")

    @classmethod
    def get_stats_list_url(cls):
        return "http://testserver" + reverse("events-stats-list")


class FeedFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Feed]
):
    class Meta:
        model = models.Feed

    event = factory.SubFactory(EventFactory)


class WebHookFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.WebHook]
):
    class Meta:
        model = models.WebHook

    event_types = get_valid_events()[:3]
    destination_url = "http://example.com/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("webhook-list")

    @classmethod
    def get_url(cls, hook=None):
        if hook is None:
            hook = WebHookFactory()
        return "http://testserver" + reverse(
            "webhook-detail", kwargs={"uuid": hook.uuid.hex}
        )


class EmailHookFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.EmailHook]
):
    class Meta:
        model = models.EmailHook

    event_types = get_valid_events()[:3]
    email = "hook@example.com"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("emailhook-list")

    @classmethod
    def get_url(cls, hook=None):
        if hook is None:
            hook = EmailHookFactory()
        return "http://testserver" + reverse(
            "emailhook-detail", kwargs={"uuid": hook.uuid.hex}
        )


class SystemNotificationFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.SystemNotification],
):
    class Meta:
        model = models.SystemNotification

    event_types = get_valid_events()[:3]
    roles = ["admin"]
    hook_content_type = factory.LazyAttribute(
        lambda o: ct_models.ContentType.objects.get_by_natural_key(
            "logging", "emailhook"
        )
    )


class EventSubscriptionFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.EventSubscription],
):
    class Meta:
        model = models.EventSubscription

    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("event-subscription-list")

    @classmethod
    def get_url(cls, subscription=None, action=None):
        if subscription is None:
            subscription = EventSubscriptionFactory()
        url = "http://testserver" + reverse(
            "event-subscription-detail", kwargs={"uuid": subscription.uuid.hex}
        )
        if action:
            url += f"{action}/"
        return url


class EmailLogFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.EmailLog]
):
    class Meta:
        model = models.EmailLog

    subject = factory.Sequence(lambda i: "subject_#%s" % i)
    body = factory.Sequence(lambda i: "body_#%s" % i)
    emails = factory.List(
        [
            factory.LazyAttribute(lambda n: f"user_{n}_1@example.com"),
            factory.LazyAttribute(lambda n: f"user_{n}_2@example.com"),
        ]
    )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("email-log-list")

    @classmethod
    def get_url(cls, email_log=None):
        if email_log is None:
            email_log = EmailLogFactory()
        return "http://testserver" + reverse(
            "email-log-detail", kwargs={"uuid": email_log.uuid.hex}
        )


class SystemLogFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SystemLog]
):
    class Meta:
        model = models.SystemLog

    source = "api"
    instance = "test-pod-1"
    level = "INFO"
    level_number = 20
    logger_name = "waldur_core.test"
    message = factory.Sequence(lambda i: "test log message #%s" % i)
    context = factory.LazyFunction(dict)

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("system-log-list")

    @classmethod
    def get_url(cls, log=None):
        if log is None:
            log = SystemLogFactory()
        return "http://testserver" + reverse("system-log-detail", kwargs={"pk": log.pk})

    @classmethod
    def get_stats_url(cls):
        return "http://testserver" + reverse("system-log-stats")

    @classmethod
    def get_instances_url(cls):
        return "http://testserver" + reverse("system-log-instances")


class EventSubscriptionQueueFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.EventSubscriptionQueue],
):
    class Meta:
        model = models.EventSubscriptionQueue

    event_subscription = factory.SubFactory(EventSubscriptionFactory)
    offering_uuid = factory.Faker("uuid4")
    object_type = "resource"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("event-subscription-queue-list")

    @classmethod
    def get_url(cls, queue=None):
        if queue is None:
            queue = EventSubscriptionQueueFactory()
        return "http://testserver" + reverse(
            "event-subscription-queue-detail", kwargs={"uuid": queue.uuid.hex}
        )
