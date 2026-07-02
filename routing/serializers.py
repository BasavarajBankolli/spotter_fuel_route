from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=255, help_text="Start location within the USA, e.g. 'Chicago, IL'"
    )
    finish = serializers.CharField(
        max_length=255, help_text="Finish location within the USA, e.g. 'Dallas, TX'"
    )
