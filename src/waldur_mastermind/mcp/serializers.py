from oauth2_provider.models import get_application_model
from rest_framework import serializers


def describe_invalid_params(errors) -> str:
    return f"Invalid parameters: {', '.join(errors)}."


class ConsentRequestSerializer(serializers.Serializer):
    """The authorization request, as the client sent it to /oauth/authorize/.

    Only these parameters reach the grant: response_mode, nonce and claims are
    not part of this flow.
    """

    client_id = serializers.CharField()
    redirect_uri = serializers.CharField(required=False)
    response_type = serializers.CharField()
    scope = serializers.CharField(required=False, allow_blank=True)
    # Returned to the client exactly as it came.
    state = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False
    )
    # The column's size, so too long a value is refused rather than failing on save.
    code_challenge = serializers.CharField(max_length=128)
    code_challenge_method = serializers.CharField()
    resource = serializers.ListField(child=serializers.CharField(), required=False)


class ConsentDecisionSerializer(ConsentRequestSerializer):
    allow = serializers.BooleanField()


class ConsentScopeSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField()


class ConsentDetailsSerializer(serializers.Serializer):
    client_name = serializers.CharField()
    client_host = serializers.CharField(allow_null=True)
    redirect_host = serializers.CharField()
    registration_source = serializers.ChoiceField(
        choices=get_application_model().RegistrationSource.choices
    )
    scopes = ConsentScopeSerializer(many=True)
    elevated_access = serializers.ChoiceField(
        choices=["staff", "support"], allow_null=True
    )


class ConsentResultSerializer(serializers.Serializer):
    redirect_url = serializers.CharField()


class ConsentErrorSerializer(serializers.Serializer):
    error = serializers.CharField()
    error_description = serializers.CharField()
