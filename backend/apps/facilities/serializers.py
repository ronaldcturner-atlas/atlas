from rest_framework import serializers

from .models import Facility


class FacilitySerializer(serializers.ModelSerializer):
	class Meta:
		model = Facility
		fields = ['id', 'name', 'short_name', 'timezone', 'color', 'active', 'sort_order']
		read_only_fields = ['id', 'sort_order']
