from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Domain
from .serializers import DomainSerializer


class CsrfExemptSessionAuthentication(SessionAuthentication):
	def enforce_csrf(self, request):
		return


def _ensure_default_domain():
	Domain.objects.get_or_create(
		name='Physician',
		defaults={'active': True},
	)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domains_list_create(request):
	if request.method == 'GET':
		_ensure_default_domain()
		domains = Domain.objects.all()

		active_filter = request.query_params.get('active')
		if active_filter in {'true', 'false'}:
			domains = domains.filter(active=active_filter == 'true')

		serializer = DomainSerializer(domains, many=True)
		return Response(serializer.data)

	serializer = DomainSerializer(data=request.data)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def domain_detail(request, domain_id):
	domain = get_object_or_404(Domain, id=domain_id)

	if request.method == 'GET':
		serializer = DomainSerializer(domain)
		return Response(serializer.data)

	partial = request.method == 'PATCH'
	serializer = DomainSerializer(domain, data=request.data, partial=partial)
	serializer.is_valid(raise_exception=True)
	serializer.save()
	return Response(serializer.data)
