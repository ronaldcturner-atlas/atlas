from rest_framework import serializers

from .models import Domain


class DomainSerializer(serializers.ModelSerializer):
	class Meta:
		model = Domain
		fields = ['id', 'name', 'active', 'created_at', 'updated_at']
		read_only_fields = ['id', 'created_at', 'updated_at']
