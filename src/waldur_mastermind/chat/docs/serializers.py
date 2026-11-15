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


class DocsSearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    sections = DocsSearchHitSerializer(many=True)
