from rest_framework import serializers

from waldur_mastermind.chat.docs import search


class DocsSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=search.MAX_QUERY_LENGTH)
    limit = serializers.IntegerField(
        min_value=1,
        max_value=search.MAX_PAGE_RESULTS,
        default=search.MAX_RESULTS,
        required=False,
    )

    def validate_query(self, value):
        return " ".join(value.split())


class DocsSearchHitSerializer(serializers.Serializer):
    title = serializers.CharField()
    breadcrumb = serializers.CharField()
    excerpt = serializers.CharField()
    url = serializers.URLField()
    enabled_here = serializers.BooleanField(
        help_text=(
            "False when the section describes a feature that is switched off "
            "on this portal, or that it cannot confirm is switched on. Only "
            "staff and support get such sections."
        )
    )


class DocsSearchSwitchedOffSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="What a user would call the feature.")
    term = serializers.CharField(
        help_text="The setting, feature flag or offering type that switches it, "
        "e.g. setting:PAT_ENABLED."
    )


class DocsSearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    sections = DocsSearchHitSerializer(many=True)
    switched_off_here = DocsSearchSwitchedOffSerializer(
        many=True,
        help_text=(
            "Features the query is about that are switched off on this portal. "
            "Their sections are not returned to users without staff or support "
            "rights; only the feature is named."
        ),
    )
