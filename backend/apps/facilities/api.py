from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Max
from django.db.models.deletion import ProtectedError
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
	next_sort_order = (Facility.objects.aggregate(max_order=Max('sort_order'))['max_order'] or 0) + 1
	serializer.save(sort_order=next_sort_order)
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def facilities_reorder(request):
	facility_ids = request.data.get('facility_ids')
	if not isinstance(facility_ids, list) or any(type(value) is not int for value in facility_ids):
		return Response(
			{'facility_ids': ['Provide the facility IDs in the desired order.']},
			status=status.HTTP_400_BAD_REQUEST,
		)

	existing_ids = list(Facility.objects.values_list('id', flat=True))
	if len(facility_ids) != len(set(facility_ids)) or set(facility_ids) != set(existing_ids):
		return Response(
			{'facility_ids': ['Include every facility exactly once.']},
			status=status.HTTP_400_BAD_REQUEST,
		)

	with transaction.atomic():
		for sort_order, facility_id in enumerate(facility_ids, start=1):
			Facility.objects.filter(id=facility_id).update(sort_order=sort_order)

	return Response(FacilitySerializer(Facility.objects.all(), many=True).data)


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def facility_detail(request, facility_id):
	facility = get_object_or_404(Facility, id=facility_id)
	if request.method == 'DELETE':
		try:
			facility.delete()
		except ProtectedError:
			return Response(
				{'error': 'This facility is already used by a schedule and cannot be deleted. Disable it instead.'},
				status=status.HTTP_409_CONFLICT,
			)
		return Response(status=status.HTTP_204_NO_CONTENT)

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


