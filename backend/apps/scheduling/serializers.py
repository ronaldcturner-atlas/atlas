from rest_framework import serializers
from .models import Shift


class ShiftSerializer(serializers.ModelSerializer):
    facility_name = serializers.CharField(source='facility.name', read_only=True)
    physician_name = serializers.CharField(source='physician.user.get_full_name', read_only=True)
    
    class Meta:
        model = Shift
        fields = [
            'id',
            'facility',
            'facility_name',
            'role',
            'physician',
            'physician_name',
            'start_datetime',
            'end_datetime',
            'status',
        ]
