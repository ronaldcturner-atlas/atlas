from datetime import datetime

from django.db.models import Q
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view
from rest_framework.decorators import authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import Physician

from .models import Contract, ScheduleBlock, ScheduleRequest, Shift, ShiftTemplate
from .serializers import ContractSerializer, ScheduleBlockSerializer, ScheduleRequestSerializer, ShiftSerializer, ShiftTemplateSerializer


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


def _can_manage_requests(user):
    if user.is_staff or user.is_superuser:
        return True

    return user.groups.filter(Q(name__iexact='admin') | Q(name__iexact='scheduler')).exists()


def _editable_request_status(block):
    return block.build_status in {ScheduleBlock.BuildStatus.PRE_BUILD, ScheduleBlock.BuildStatus.BUILD}


def _resolve_self_physician(user):
    try:
        return user.physician
    except Physician.DoesNotExist:
        return None


def _parse_request_date(raw_value):
    if not raw_value or not isinstance(raw_value, str):
        return None

    try:
        return datetime.strptime(raw_value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _get_available_shift_templates_for_date(target_date):
    day_name = target_date.strftime('%A')
    templates = ShiftTemplate.objects.select_related('facility').filter(active=True)
    return [template for template in templates if day_name in (template.active_days_of_week or [])]


def _serialize_physician_choice(physician):
    display_name = physician.display_name or physician.user.get_full_name() or physician.user.username
    return {
        'id': physician.id,
        'name': display_name,
    }


def _validate_request_payload(request_type, weight, shift_template_ids, available_template_ids):
    allowed_types = {choice[0] for choice in ScheduleRequest.RequestType.choices}
    if request_type not in allowed_types:
        return {'request_type': 'Invalid request type.'}

    allowed_weights = {choice[0] for choice in ScheduleRequest.Weight.choices}
    if weight not in allowed_weights:
        return {'weight': 'Weight is required and must be one of LOW, MEDIUM, HIGH, or FIXED.'}

    if request_type in {ScheduleRequest.RequestType.DAY_OFF, ScheduleRequest.RequestType.DAY_ON}:
        if shift_template_ids:
            return {'shift_template_ids': 'Do not select shift templates for Day Off or Day On requests.'}
        return None

    if request_type == ScheduleRequest.RequestType.SHIFT_OFF and not shift_template_ids:
        return {'shift_template_ids': 'Select one or more shift templates for Shift Off requests.'}

    if request_type == ScheduleRequest.RequestType.SHIFT_ON and len(shift_template_ids) != 1:
        return {'shift_template_ids': 'Select exactly one shift template for Shift On requests.'}

    invalid_template_ids = [template_id for template_id in shift_template_ids if template_id not in available_template_ids]
    if invalid_template_ids:
        return {'shift_template_ids': 'One or more selected shift templates are not available on this date.'}

    return None


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_requests_context(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    can_manage = _can_manage_requests(request.user)

    if can_manage:
        physicians = list(
            Physician.objects.select_related('user').order_by('user__last_name', 'user__first_name')
        )
    else:
        self_physician = _resolve_self_physician(request.user)
        physicians = [self_physician] if self_physician else []

    selected_physician_id_param = request.query_params.get('physician_id')
    selected_physician_id = physicians[0].id if physicians else None

    if selected_physician_id_param:
        try:
            requested_physician_id = int(selected_physician_id_param)
        except (TypeError, ValueError):
            return Response({'physician_id': 'physician_id must be a valid integer.'}, status=status.HTTP_400_BAD_REQUEST)

        if any(physician.id == requested_physician_id for physician in physicians):
            selected_physician_id = requested_physician_id
        else:
            return Response({'detail': 'You do not have permission to view requests for this physician.'}, status=status.HTTP_403_FORBIDDEN)

    request_items = ScheduleRequest.objects.filter(
        schedule_block=block,
        physician_id=selected_physician_id,
    ).prefetch_related('shift_templates') if selected_physician_id else ScheduleRequest.objects.none()

    serialized_templates = ShiftTemplateSerializer(
        ShiftTemplate.objects.select_related('facility').filter(active=True),
        many=True,
    ).data

    return Response(
        {
            'schedule_block': ScheduleBlockSerializer(block).data,
            'can_manage_requests': can_manage,
            'selected_physician_id': selected_physician_id,
            'physicians': [_serialize_physician_choice(physician) for physician in physicians],
            'requests': ScheduleRequestSerializer(request_items, many=True).data,
            'shift_templates': serialized_templates,
        }
    )


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_request_upsert(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be entered for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    can_manage = _can_manage_requests(request.user)
    self_physician = _resolve_self_physician(request.user)

    physician_id = request.data.get('physician_id')
    try:
        physician_id = int(physician_id)
    except (TypeError, ValueError):
        return Response({'physician_id': 'physician_id is required and must be a valid integer.'}, status=status.HTTP_400_BAD_REQUEST)

    if can_manage:
        physician = get_object_or_404(Physician.objects.select_related('user'), id=physician_id)
    else:
        if not self_physician:
            return Response({'detail': 'Authenticated user is not linked to a physician profile.'}, status=status.HTTP_403_FORBIDDEN)
        if physician_id != self_physician.id:
            return Response({'detail': 'You do not have permission to modify requests for this physician.'}, status=status.HTTP_403_FORBIDDEN)
        physician = self_physician

    parsed_date = _parse_request_date(request.data.get('date'))
    if not parsed_date:
        return Response({'date': 'date is required and must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)

    if parsed_date < block.start_date or parsed_date > block.end_date:
        return Response({'date': 'Date must be within the selected Schedule Block range.'}, status=status.HTTP_400_BAD_REQUEST)

    request_scope = str(request.data.get('request_scope') or ScheduleRequest.RequestScope.USER).upper()
    allowed_scopes = {choice[0] for choice in ScheduleRequest.RequestScope.choices}
    if request_scope not in allowed_scopes:
        return Response({'request_scope': 'Invalid request scope.'}, status=status.HTTP_400_BAD_REQUEST)

    if request_scope == ScheduleRequest.RequestScope.ADMIN and not can_manage:
        return Response({'detail': 'Only admin/scheduler users can create admin requests.'}, status=status.HTTP_403_FORBIDDEN)

    request_type = str(request.data.get('request_type') or '').upper()
    if request_type == 'NONE':
        deleted, _ = ScheduleRequest.objects.filter(
            schedule_block=block,
            physician=physician,
            date=parsed_date,
            request_scope=request_scope,
        ).delete()
        return Response({'deleted': bool(deleted)})

    weight = str(request.data.get('weight') or '').upper()

    raw_shift_template_ids = request.data.get('shift_template_ids') or []
    if not isinstance(raw_shift_template_ids, list):
        return Response({'shift_template_ids': 'shift_template_ids must be an array of ids.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        shift_template_ids = sorted({int(template_id) for template_id in raw_shift_template_ids})
    except (TypeError, ValueError):
        return Response({'shift_template_ids': 'shift_template_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    available_templates = _get_available_shift_templates_for_date(parsed_date)
    available_template_ids = {template.id for template in available_templates}

    payload_error = _validate_request_payload(request_type, weight, shift_template_ids, available_template_ids)
    if payload_error:
        return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)

    schedule_request, _ = ScheduleRequest.objects.get_or_create(
        schedule_block=block,
        physician=physician,
        date=parsed_date,
        request_scope=request_scope,
        defaults={
            'request_type': request_type,
            'weight': weight,
            'created_by': request.user,
        },
    )

    schedule_request.request_type = request_type
    schedule_request.weight = weight
    schedule_request.created_by = request.user
    schedule_request.save()

    if shift_template_ids:
        schedule_request.shift_templates.set(ShiftTemplate.objects.filter(id__in=shift_template_ids))
    else:
        schedule_request.shift_templates.clear()

    return Response(ScheduleRequestSerializer(schedule_request).data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_bulk_requests(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _editable_request_status(block):
        return Response(
            {'detail': 'Bulk requests can only be entered for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not _can_manage_requests(request.user):
        return Response({'detail': 'Only admin/scheduler users can create bulk requests.'}, status=status.HTTP_403_FORBIDDEN)

    request_scope = str(request.data.get('request_scope') or ScheduleRequest.RequestScope.USER).upper()
    allowed_scopes = {choice[0] for choice in ScheduleRequest.RequestScope.choices}
    if request_scope not in allowed_scopes:
        return Response({'request_scope': 'Invalid request scope.'}, status=status.HTTP_400_BAD_REQUEST)

    request_type = str(request.data.get('request_type') or '').upper()
    if request_type == 'NONE':
        return Response({'request_type': 'Bulk action does not support NONE.'}, status=status.HTTP_400_BAD_REQUEST)

    weight = str(request.data.get('weight') or '').upper()

    physician_ids = request.data.get('physician_ids') or []
    if not isinstance(physician_ids, list) or not physician_ids:
        return Response({'physician_ids': 'Select one or more physicians.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        physician_ids = sorted({int(physician_id) for physician_id in physician_ids})
    except (TypeError, ValueError):
        return Response({'physician_ids': 'physician_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    physicians = list(Physician.objects.filter(id__in=physician_ids))
    if len(physicians) != len(physician_ids):
        return Response({'physician_ids': 'One or more physicians were not found.'}, status=status.HTTP_400_BAD_REQUEST)

    raw_dates = request.data.get('dates') or []
    if not isinstance(raw_dates, list) or not raw_dates:
        return Response({'dates': 'Select one or more dates.'}, status=status.HTTP_400_BAD_REQUEST)

    parsed_dates = []
    for raw_date in raw_dates:
        parsed_date = _parse_request_date(raw_date)
        if not parsed_date:
            return Response({'dates': 'All dates must be in YYYY-MM-DD format.'}, status=status.HTTP_400_BAD_REQUEST)
        if parsed_date < block.start_date or parsed_date > block.end_date:
            return Response({'dates': 'All dates must be within the selected Schedule Block range.'}, status=status.HTTP_400_BAD_REQUEST)
        parsed_dates.append(parsed_date)

    parsed_dates = sorted(set(parsed_dates))

    raw_shift_template_ids = request.data.get('shift_template_ids') or []
    if not isinstance(raw_shift_template_ids, list):
        return Response({'shift_template_ids': 'shift_template_ids must be an array of ids.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        shift_template_ids = sorted({int(template_id) for template_id in raw_shift_template_ids})
    except (TypeError, ValueError):
        return Response({'shift_template_ids': 'shift_template_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    template_ids_by_date = {}
    for parsed_date in parsed_dates:
        available_template_ids = {template.id for template in _get_available_shift_templates_for_date(parsed_date)}
        payload_error = _validate_request_payload(request_type, weight, shift_template_ids, available_template_ids)
        if payload_error:
            payload_error['date'] = parsed_date.isoformat()
            return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)
        template_ids_by_date[parsed_date] = shift_template_ids

    template_queryset = ShiftTemplate.objects.filter(id__in=shift_template_ids)

    saved_count = 0
    with transaction.atomic():
        for physician in physicians:
            for parsed_date in parsed_dates:
                schedule_request, _ = ScheduleRequest.objects.get_or_create(
                    schedule_block=block,
                    physician=physician,
                    date=parsed_date,
                    request_scope=request_scope,
                    defaults={
                        'request_type': request_type,
                        'weight': weight,
                        'created_by': request.user,
                    },
                )
                schedule_request.request_type = request_type
                schedule_request.weight = weight
                schedule_request.created_by = request.user
                schedule_request.save()
                if template_ids_by_date[parsed_date]:
                    schedule_request.shift_templates.set(template_queryset)
                else:
                    schedule_request.shift_templates.clear()
                saved_count += 1

    return Response({'saved_count': saved_count, 'request_scope': request_scope})


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


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def contracts_list_create(request):
    if request.method == 'GET':
        contracts = Contract.objects.select_related('domain').prefetch_related('facilities', 'user_assignments__physician__user').all()

        domain_id = request.query_params.get('domain')
        include_inactive = request.query_params.get('include_inactive') == 'true'
        search = (request.query_params.get('search') or '').strip()

        if domain_id:
            contracts = contracts.filter(domain_id=domain_id)

        if not include_inactive:
            contracts = contracts.filter(active=True)

        if search:
            contracts = contracts.filter(name__icontains=search)

        serializer = ContractSerializer(contracts, many=True)
        return Response(serializer.data)

    serializer = ContractSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    contract = serializer.save()
    return Response(ContractSerializer(contract).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_detail(request, contract_id):
    contract = get_object_or_404(
        Contract.objects.select_related('domain').prefetch_related('facilities', 'user_assignments__physician__user'),
        id=contract_id,
    )

    if request.method == 'GET':
        serializer = ContractSerializer(contract)
        return Response(serializer.data)

    partial = request.method == 'PATCH'
    serializer = ContractSerializer(contract, data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    contract.refresh_from_db()
    return Response(ContractSerializer(contract).data)


def _copy_json_dict(source_value):
    if isinstance(source_value, dict):
        return dict(source_value)
    return {}


def _build_duplicate_contract_name(source_contract):
    base_name = f'{source_contract.name} (Copy)'
    next_name = base_name
    suffix = 2

    while Contract.objects.filter(domain=source_contract.domain, name=next_name).exists():
        next_name = f'{base_name} {suffix}'
        suffix += 1

    return next_name


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_duplicate(request, contract_id):
    source_contract = get_object_or_404(
        Contract.objects.select_related('domain').prefetch_related('facilities', 'user_assignments'),
        id=contract_id,
    )

    with transaction.atomic():
        duplicate = Contract.objects.create(
            domain=source_contract.domain,
            name=_build_duplicate_contract_name(source_contract),
            active=False,
            workload_settings=_copy_json_dict(source_contract.workload_settings),
            shift_settings=_copy_json_dict(source_contract.shift_settings),
            night_settings=_copy_json_dict(source_contract.night_settings),
            weekend_settings=_copy_json_dict(source_contract.weekend_settings),
            request_settings=_copy_json_dict(source_contract.request_settings),
        )
        duplicate.facilities.set(source_contract.facilities.all())

    serializer = ContractSerializer(duplicate)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_deactivate(request, contract_id):
    contract = get_object_or_404(Contract, id=contract_id)
    contract.active = False
    contract.save(update_fields=['active', 'updated_at'])
    return Response(ContractSerializer(contract).data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def contract_reactivate(request, contract_id):
    contract = get_object_or_404(Contract, id=contract_id)
    contract.active = True
    contract.save(update_fields=['active', 'updated_at'])
    return Response(ContractSerializer(contract).data)
