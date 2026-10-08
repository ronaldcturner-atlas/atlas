from rest_framework import serializers

from .models import Facility


class FacilitySerializer(serializers.ModelSerializer):
	region_name = serializers.CharField(source='region.name', read_only=True)
	organization = serializers.IntegerField(source='region.organization_id', read_only=True)
	organization_name = serializers.CharField(source='region.organization.name', read_only=True)

	class Meta:
		model = Facility
		fields = ['id', 'region', 'region_name', 'organization', 'organization_name', 'name', 'short_name', 'timezone', 'color', 'active', 'sort_order']
		read_only_fields = ['id', 'region_name', 'organization', 'organization_name', 'sort_order']

	def validate(self, attrs):
		attrs = super().validate(attrs)
		if (
			self.instance is not None
			and 'region' in attrs
			and attrs['region'].id != self.instance.region_id
		):
			raise serializers.ValidationError({
				'region': 'A Facility cannot be moved to another Region.',
			})
		return attrs
