from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ScheduleBlock, Shift, ShiftTemplate
from .serializers import ScheduleBlockSerializer, ShiftSerializer, ShiftTemplateSerializer


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def shifts_list_create(request):
    if request.method == 'GET':
        shifts = Shift.objects.select_related('facility', 'physician', 'physician__user').all()

        facility_id = request.query_params.get('facility')
        physician_id = request.query_params.get('physician')
        month = request.query_params.get('month')
        status_filter = request.query_params.get('status')
        search = request.query_params.get('search')

        if facility_id:
            shifts = shifts.filter(facility_id=facility_id)

        if physician_id:
            shifts = shifts.filter(physician_id=physician_id)

        if month:
            try:
                year_str, month_str = month.split('-', 1)
                shifts = shifts.filter(date__year=int(year_str), date__month=int(month_str))
            except ValueError:
                return Response(
                    {'month': 'Invalid month format. Use YYYY-MM.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if status_filter:
            shifts = shifts.filter(status=status_filter)

        if search:
            shifts = shifts.filter(
                Q(physician__user__first_name__icontains=search)
                | Q(physician__user__last_name__icontains=search)
                | Q(physician__display_name__icontains=search)
                | Q(facility__name__icontains=search)
                | Q(role__icontains=search)
                | Q(notes__icontains=search)
            )

        serializer = ShiftSerializer(shifts.distinct(), many=True)
        return Response(serializer.data)

    serializer = ShiftSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_detail(request, shift_id):
    shift = get_object_or_404(
        Shift.objects.select_related('facility', 'physician', 'physician__user'),
        id=shift_id,
    )

    if request.method == 'GET':
        serializer = ShiftSerializer(shift)
        return Response(serializer.data)

    if request.method in ['PUT', 'PATCH']:
        partial = request.method == 'PATCH'
        serializer = ShiftSerializer(shift, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    shift.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_templates_list_create(request):
    if request.method == 'GET':
        templates = ShiftTemplate.objects.select_related('facility').all()

        facility_id = request.query_params.get('facility')
        active_filter = request.query_params.get('active')
        search = request.query_params.get('search')

        if facility_id:
            templates = templates.filter(facility_id=facility_id)

        if active_filter in {'true', 'false'}:
            templates = templates.filter(active=active_filter == 'true')

        if search:
            templates = templates.filter(
                Q(name__icontains=search)
                | Q(facility__name__icontains=search)
            )

        serializer = ShiftTemplateSerializer(templates, many=True)
        return Response(serializer.data)

    serializer = ShiftTemplateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def shift_template_detail(request, template_id):
    template = get_object_or_404(
        ShiftTemplate.objects.select_related('facility'),
        id=template_id,
    )

    if request.method == 'GET':
        serializer = ShiftTemplateSerializer(template)
        return Response(serializer.data)

    partial = request.method == 'PATCH'
    serializer = ShiftTemplateSerializer(template, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(serializer.data)


def _has_published_overlap(start_date, end_date, exclude_id=None):
    published_blocks = ScheduleBlock.objects.filter(published_at__isnull=False)
    if exclude_id is not None:
        published_blocks = published_blocks.exclude(id=exclude_id)
    return published_blocks.filter(start_date__lte=end_date, end_date__gte=start_date).exists()


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_blocks_list_create(request):
    if request.method == 'GET':
        blocks = ScheduleBlock.objects.all()
        serializer = ScheduleBlockSerializer(blocks, many=True)
        return Response(serializer.data)

    serializer = ScheduleBlockSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    start_date = serializer.validated_data['start_date']
    end_date = serializer.validated_data['end_date']
    acknowledged_overlap = bool(request.data.get('acknowledge_overlap', False))

    if _has_published_overlap(start_date, end_date) and not acknowledged_overlap:
        return Response(
            {
                'warning': (
                    'A published Schedule Block already exists for one or more dates in this period. '
                    'If this Schedule Block is later published it will replace the existing Live '
                    'Schedule for those dates.'
                ),
                'requires_acknowledgement': True,
            },
            status=status.HTTP_409_CONFLICT,
        )

    block = serializer.save(build_status=ScheduleBlock.BuildStatus.PRE_BUILD)
    return Response(ScheduleBlockSerializer(block).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_detail(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)

    if request.method == 'GET':
        serializer = ScheduleBlockSerializer(block)
        return Response(serializer.data)

    if request.method == 'PATCH':
        partial = True
        serializer = ScheduleBlockSerializer(block, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    if block.build_status != ScheduleBlock.BuildStatus.PRE_BUILD:
        return Response(
            {'detail': 'Only PRE_BUILD Schedule Blocks can be deleted.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    block.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_enter_preview(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)

    if block.build_status == ScheduleBlock.BuildStatus.ARCHIVE:
        return Response({'detail': 'Archived Schedule Blocks cannot enter preview.'}, status=status.HTTP_400_BAD_REQUEST)

    if block.build_status == ScheduleBlock.BuildStatus.PREVIEW:
        return Response(ScheduleBlockSerializer(block).data)

    # Request intake is out of scope; allow PRE_BUILD to progress into PREVIEW for lifecycle testing.
    if block.build_status in {ScheduleBlock.BuildStatus.PRE_BUILD, ScheduleBlock.BuildStatus.BUILD}:
        block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        block.save(update_fields=['build_status', 'updated_at'])
        return Response(ScheduleBlockSerializer(block).data)

    return Response({'detail': 'Schedule Block cannot enter preview from its current state.'}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_publish(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)

    if block.build_status != ScheduleBlock.BuildStatus.PREVIEW:
        return Response({'detail': 'Only PREVIEW Schedule Blocks can be published.'}, status=status.HTTP_400_BAD_REQUEST)

    acknowledged_overlap = bool(request.data.get('acknowledge_overlap', False))
    if _has_published_overlap(block.start_date, block.end_date, exclude_id=block.id) and not acknowledged_overlap:
        return Response(
            {
                'warning': (
                    'You are about to replace an existing Live Schedule for one or more dates.\n\n'
                    'The previous published Schedule Block will remain archived as the historical '
                    'schedule of record.\n\n'
                    'Continue?'
                ),
                'requires_acknowledgement': True,
            },
            status=status.HTTP_409_CONFLICT,
        )

    block.published_at = timezone.now()
    block.build_status = ScheduleBlock.BuildStatus.ARCHIVE
    block.save(update_fields=['published_at', 'build_status', 'updated_at'])
    return Response(ScheduleBlockSerializer(block).data)
