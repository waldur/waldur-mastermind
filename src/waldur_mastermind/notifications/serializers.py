from rest_framework import serializers

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import Offering

from . import models, utils


class QuerySerializer(serializers.Serializer):
    customers = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Customer.objects.all(),
        many=True,
        required=False,
    )
    offerings = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Offering.objects.all(),
        many=True,
        required=False,
    )
    all_users = serializers.BooleanField(default=False)


def format_options(options):
    return [{"name": option.name, "uuid": option.uuid.hex} for option in options]


def serialize_query(query):
    serialized_query = {}
    if "customers" in query:
        serialized_query["customers"] = format_options(query["customers"])
    if "offerings" in query:
        serialized_query["offerings"] = format_options(query["offerings"])
    serialized_query["all_users"] = query.get("all_users", False)
    return serialized_query


class BroadcastMessageSerializer(serializers.ModelSerializer):
    author_full_name = serializers.ReadOnlyField(source="author.full_name")
    state = serializers.ReadOnlyField()
    emails = serializers.ReadOnlyField()

    class Meta:
        model = models.BroadcastMessage
        fields = (
            "uuid",
            "created",
            "subject",
            "body",
            "query",
            "author_full_name",
            "emails",
            "state",
            "send_at",
        )

    def validate_query(self, query):
        serializer = QuerySerializer(data=query)
        serializer.is_valid()
        return serializer.validated_data

    def create(self, validated_data):
        current_user = self.context["request"].user
        validated_data["author"] = current_user
        matching_users = utils.get_users_for_query(validated_data["query"])
        validated_data["emails"] = [user.email for user in matching_users if user.email]
        validated_data["query"] = serialize_query(validated_data["query"])
        return super().create(validated_data)

    def update(self, instance, validated_data):
        matching_users = utils.get_users_for_query(validated_data["query"])
        validated_data["emails"] = [user.email for user in matching_users if user.email]
        validated_data["query"] = serialize_query(validated_data["query"])
        return super().update(instance, validated_data)


class MessageTemplateSerializer(
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.MessageTemplate
        fields = (
            "url",
            "uuid",
            "name",
            "subject",
            "body",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }
