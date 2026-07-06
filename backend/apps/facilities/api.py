from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Facility
from .serializers import FacilitySerializer


class CsrfExemptSessionAuthentication(SessionAuthentication):
	def enforce_csrf(self, request):
		return


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def facilities_list_create(request):
	if request.method == 'GET':
		facilities = Facility.objects.all()

		active_filter = request.query_params.get('active')
		if active_filter in {'true', 'false'}:
			facilities = facilities.filter(active=active_filter == 'true')

		serializer = FacilitySerializer(facilities, many=True)
		return Response(serializer.data)

	serializer = FacilitySerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def facility_detail(request, facility_id):
	facility = get_object_or_404(Facility, id=facility_id)

	if request.method == 'GET':
		serializer = FacilitySerializer(facility)
		return Response(serializer.data)

	partial = request.method == 'PATCH'
	serializer = FacilitySerializer(facility, data=request.data, partial=partial)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def facility_disable(request, facility_id):
	facility = get_object_or_404(Facility, id=facility_id)
	facility.active = False
	facility.save(update_fields=['active'])
	serializer = FacilitySerializer(facility)
	return Response(serializer.data)


