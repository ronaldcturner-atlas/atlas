from datetime import datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from apps.domains.models import Domain

from .models import (
    Contract,
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    Shift,
    ShiftTemplate,
)
from .optimizer import build_violation_report, optimize_schedule_version
from .serializers import (
    ContractSerializer,
    OptimizerRunSerializer,
    ScheduleBlockSerializer,
    ScheduleRequestSerializer,
    ScheduleShiftInstanceSerializer,
    ScheduleVersionSerializer,
    ShiftSerializer,
    ShiftTemplateSerializer,
)


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

    if user.groups.filter(Q(name__iexact='admin') | Q(name__iexact='scheduler')).exists():
        return True

    return user.has_perm('scheduling.add_schedulerequest') or user.has_perm(
        'scheduling.change_schedulerequest'
    )


def _can_manage_build_workspace(user):
    if user.is_staff or user.is_superuser:
        return True

    if user.groups.filter(Q(name__iexact='admin') | Q(name__iexact='scheduler')).exists():
        return True

    return user.has_perm('scheduling.add_scheduleversion') or user.has_perm(
        'scheduling.change_scheduleversion'
    )


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


def _get_request_contract(physician):
    """Return the single active contract when Request Builder can resolve one unambiguously."""
    contracts = list(
        Contract.objects.filter(
            active=True,
            user_assignments__physician=physician,
        )
        .prefetch_related('facilities')
        .distinct()[:2]
    )
    return contracts[0] if len(contracts) == 1 else None


def _parse_request_limit(value):
    if value in (None, ''):
        return None

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    return parsed if parsed >= 0 else None


def _get_request_policy(physician, can_manage=False):
    all_types = [choice[0] for choice in ScheduleRequest.RequestType.choices]
    contract = _get_request_contract(physician)

    if contract is None:
        return {
            'contract_id': None,
            'contract_name': None,
            'allowed_request_types': all_types if can_manage else [],
            'eligible_facility_ids': None,
            'limits': {
                'HIGH': None,
                'MEDIUM': None,
                'LOW': None,
                'WEEKEND': None,
            },
            'low_unlimited': True,
        }

    settings = contract.request_settings if isinstance(contract.request_settings, dict) else {}
    setting_names = {
        ScheduleRequest.RequestType.DAY_OFF: 'allow_day_off',
        ScheduleRequest.RequestType.SHIFT_OFF: 'allow_shift_off',
        ScheduleRequest.RequestType.DAY_ON: 'allow_day_on',
        ScheduleRequest.RequestType.SHIFT_ON: 'allow_shift_on',
    }
    contract_allowed_types = [
        request_type
        for request_type in all_types
        if settings.get(setting_names[request_type], True) is True
    ]

    return {
        'contract_id': contract.id,
        'contract_name': contract.name,
        'allowed_request_types': all_types if can_manage else contract_allowed_types,
        'eligible_facility_ids': list(contract.facilities.values_list('id', flat=True)),
        'limits': {
            'HIGH': _parse_request_limit(settings.get('high_request_limit')),
            'MEDIUM': _parse_request_limit(settings.get('medium_request_limit')),
            'LOW': _parse_request_limit(settings.get('low_request_limit')),
            'WEEKEND': _parse_request_limit(settings.get('weekend_request_limit')),
        },
        'low_unlimited': bool(settings.get('low_request_unlimited', False)),
    }


def _get_available_shift_templates_for_date(target_date, eligible_facility_ids=None):
    day_name = target_date.strftime('%A')
    templates = ShiftTemplate.objects.select_related('facility').filter(active=True)
    if eligible_facility_ids is not None:
        templates = templates.filter(facility_id__in=eligible_facility_ids)
    return [template for template in templates if day_name in (template.active_days_of_week or [])]


def _request_counts_as_weekend(schedule_request, eligible_facility_ids=None):
    day_name = schedule_request.date.strftime('%A')

    if schedule_request.request_type == ScheduleRequest.RequestType.DAY_OFF:
        available_templates = _get_available_shift_templates_for_date(
            schedule_request.date,
            eligible_facility_ids,
        )
        return any(day_name in (template.weekend_days or []) for template in available_templates)

    if schedule_request.request_type == ScheduleRequest.RequestType.SHIFT_OFF:
        return any(
            day_name in (template.weekend_days or [])
            for template in schedule_request.shift_templates.all()
        )

    return False


def _build_request_counters(block, physician, policy, exclude_request_ids=None):
    exclude_request_ids = exclude_request_ids or []
    requests = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            physician=physician,
            request_scope=ScheduleRequest.RequestScope.USER,
        )
        .exclude(id__in=exclude_request_ids)
        .prefetch_related('shift_templates__facility')
    )

    used = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0, 'WEEKEND': 0}
    for schedule_request in requests:
        if schedule_request.weight in used:
            used[schedule_request.weight] += 1
        if _request_counts_as_weekend(schedule_request, policy['eligible_facility_ids']):
            used['WEEKEND'] += 1

    return {
        key: {
            'used': count,
            'limit': policy['limits'][key],
            'unlimited': key == 'LOW' and policy['low_unlimited'],
        }
        for key, count in used.items()
    }


def _request_counter_increments(request_date, request_type, weight, selected_templates, policy):
    increments = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0, 'WEEKEND': 0}
    if weight in increments:
        increments[weight] = 1

    day_name = request_date.strftime('%A')
    if request_type == ScheduleRequest.RequestType.DAY_OFF:
        available_templates = _get_available_shift_templates_for_date(
            request_date,
            policy['eligible_facility_ids'],
        )
        increments['WEEKEND'] = int(
            any(day_name in (template.weekend_days or []) for template in available_templates)
        )
    elif request_type == ScheduleRequest.RequestType.SHIFT_OFF:
        increments['WEEKEND'] = int(
            any(day_name in (template.weekend_days or []) for template in selected_templates)
        )

    return increments


def _prospective_request_limit_error(
    block,
    physician,
    policy,
    request_date,
    request_type,
    weight,
    selected_templates,
    request_scope,
    exclude_request_ids=None,
):
    if request_scope != ScheduleRequest.RequestScope.USER:
        return None

    counters = _build_request_counters(block, physician, policy, exclude_request_ids)
    increments = _request_counter_increments(
        request_date,
        request_type,
        weight,
        selected_templates,
        policy,
    )

    for key, increment in increments.items():
        if not increment:
            continue
        counter = counters[key]
        if counter['unlimited']:
            continue
        if counter['limit'] is not None and counter['used'] + increment > counter['limit']:
            return {
                'request_limit': (
                    f'{key.title()} request limit of {counter["limit"]} has been reached.'
                )
            }

    return None


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
def schedule_block_requests_list(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    requests = (
        ScheduleRequest.objects.filter(schedule_block=block)
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
    )

    if not _can_manage_requests(request.user):
        physician = _resolve_self_physician(request.user)
        if physician is None:
            requests = ScheduleRequest.objects.none()
        else:
            requests = requests.filter(
                physician=physician,
                request_scope=ScheduleRequest.RequestScope.USER,
            )

    return Response(ScheduleRequestSerializer(requests, many=True).data)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_requests_context(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    can_manage = _can_manage_requests(request.user)

    if can_manage:
        physicians = list(
            Physician.objects.filter(active=True)
            .select_related('user')
            .order_by('user__last_name', 'user__first_name')
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

    request_items = (
        ScheduleRequest.objects.filter(
            schedule_block=block,
            physician_id=selected_physician_id,
        )
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
        if selected_physician_id
        else ScheduleRequest.objects.none()
    )
    if not can_manage:
        request_items = request_items.filter(request_scope=ScheduleRequest.RequestScope.USER)

    selected_physician = next(
        (physician for physician in physicians if physician.id == selected_physician_id),
        None,
    )
    policy = _get_request_policy(selected_physician, can_manage) if selected_physician else None

    visible_requests = (
        ScheduleRequest.objects.filter(schedule_block=block)
        .select_related('physician__user')
        .prefetch_related('shift_templates__facility')
        if can_manage
        else request_items
    )

    templates = ShiftTemplate.objects.select_related('facility').filter(active=True)
    if policy and policy['eligible_facility_ids'] is not None:
        templates = templates.filter(facility_id__in=policy['eligible_facility_ids'])

    serialized_templates = ShiftTemplateSerializer(templates, many=True).data
    counters = (
        _build_request_counters(block, selected_physician, policy)
        if selected_physician and policy
        else {
            key: {'used': 0, 'limit': None, 'unlimited': key == 'LOW'}
            for key in ['HIGH', 'MEDIUM', 'LOW', 'WEEKEND']
        }
    )

    return Response(
        {
            'schedule_block': ScheduleBlockSerializer(block).data,
            'can_manage_requests': can_manage,
            'is_scheduler_or_admin': can_manage,
            'selected_physician_id': selected_physician_id,
            'physicians': [_serialize_physician_choice(physician) for physician in physicians],
            'requests': ScheduleRequestSerializer(request_items, many=True).data,
            'visible_requests': ScheduleRequestSerializer(visible_requests, many=True).data,
            'shift_templates': serialized_templates,
            'request_policy': policy,
            'request_counters': counters,
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
    policy = _get_request_policy(physician, can_manage)

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

    if not can_manage and request_type not in policy['allowed_request_types']:
        return Response(
            {'request_type': 'This request type is not allowed by your contract.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    weight = str(request.data.get('weight') or '').upper()

    raw_shift_template_ids = request.data.get('shift_template_ids') or []
    if not isinstance(raw_shift_template_ids, list):
        return Response({'shift_template_ids': 'shift_template_ids must be an array of ids.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        shift_template_ids = sorted({int(template_id) for template_id in raw_shift_template_ids})
    except (TypeError, ValueError):
        return Response({'shift_template_ids': 'shift_template_ids must contain only integer ids.'}, status=status.HTTP_400_BAD_REQUEST)

    available_templates = _get_available_shift_templates_for_date(
        parsed_date,
        policy['eligible_facility_ids'],
    )
    available_template_ids = {template.id for template in available_templates}

    payload_error = _validate_request_payload(request_type, weight, shift_template_ids, available_template_ids)
    if payload_error:
        return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)

    selected_templates = [
        template for template in available_templates if template.id in shift_template_ids
    ]
    existing_request = ScheduleRequest.objects.filter(
        schedule_block=block,
        physician=physician,
        date=parsed_date,
        request_scope=request_scope,
    ).first()
    limit_error = _prospective_request_limit_error(
        block,
        physician,
        policy,
        parsed_date,
        request_type,
        weight,
        selected_templates,
        request_scope,
        [existing_request.id] if existing_request else None,
    )
    if limit_error:
        return Response(limit_error, status=status.HTTP_400_BAD_REQUEST)

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


@api_view(['GET', 'DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_request_detail(request, block_id, request_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    schedule_request = get_object_or_404(
        ScheduleRequest.objects.select_related('physician__user').prefetch_related('shift_templates__facility'),
        id=request_id,
        schedule_block=block,
    )

    can_manage = _can_manage_requests(request.user)
    self_physician = _resolve_self_physician(request.user)
    if not can_manage and (
        self_physician is None
        or schedule_request.physician_id != self_physician.id
        or schedule_request.request_scope != ScheduleRequest.RequestScope.USER
    ):
        return Response({'detail': 'You do not have permission to access this request.'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        return Response(ScheduleRequestSerializer(schedule_request).data)

    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be removed from PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    schedule_request.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_clear_requests(request, block_id):
    block = get_object_or_404(ScheduleBlock, id=block_id)
    if not _can_manage_requests(request.user):
        return Response(
            {'detail': 'Only admin/scheduler users can clear Schedule Block requests.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not _editable_request_status(block):
        return Response(
            {'detail': 'Requests can only be cleared from PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request_scope = str(request.data.get('request_scope') or '').upper()
    if request_scope not in {
        ScheduleRequest.RequestScope.USER,
        ScheduleRequest.RequestScope.ADMIN,
    }:
        return Response(
            {'request_scope': 'request_scope must be USER or ADMIN.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    deleted_count, _ = ScheduleRequest.objects.filter(
        schedule_block=block,
        request_scope=request_scope,
    ).delete()
    return Response({'deleted_count': deleted_count, 'request_scope': request_scope})


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

    plans = {}
    for physician in physicians:
        policy = _get_request_policy(physician, can_manage=True)
        existing_target_ids = list(
            ScheduleRequest.objects.filter(
                schedule_block=block,
                physician=physician,
                date__in=parsed_dates,
                request_scope=request_scope,
            ).values_list('id', flat=True)
        )
        projected_counters = _build_request_counters(
            block,
            physician,
            policy,
            existing_target_ids if request_scope == ScheduleRequest.RequestScope.USER else None,
        )

        for parsed_date in parsed_dates:
            available_templates = _get_available_shift_templates_for_date(
                parsed_date,
                policy['eligible_facility_ids'],
            )
            available_template_ids = {template.id for template in available_templates}
            payload_error = _validate_request_payload(
                request_type,
                weight,
                shift_template_ids,
                available_template_ids,
            )
            if payload_error:
                payload_error['date'] = parsed_date.isoformat()
                payload_error['physician_id'] = physician.id
                return Response(payload_error, status=status.HTTP_400_BAD_REQUEST)

            selected_templates = [
                template for template in available_templates if template.id in shift_template_ids
            ]
            plans[(physician.id, parsed_date)] = selected_templates

            if request_scope != ScheduleRequest.RequestScope.USER:
                continue

            increments = _request_counter_increments(
                parsed_date,
                request_type,
                weight,
                selected_templates,
                policy,
            )
            for key, increment in increments.items():
                if not increment:
                    continue
                counter = projected_counters[key]
                if not counter['unlimited'] and counter['limit'] is not None:
                    if counter['used'] + increment > counter['limit']:
                        return Response(
                            {
                                'request_limit': (
                                    f'{key.title()} request limit of {counter["limit"]} '
                                    f'has been reached for {_serialize_physician_choice(physician)["name"]}.'
                                ),
                                'physician_id': physician.id,
                                'date': parsed_date.isoformat(),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                counter['used'] += increment

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
                selected_templates = plans[(physician.id, parsed_date)]
                if selected_templates:
                    schedule_request.shift_templates.set(selected_templates)
                else:
                    schedule_request.shift_templates.clear()
                saved_count += 1

    return Response({'saved_count': saved_count, 'request_scope': request_scope})


def _build_workspace_forbidden_response():
    return Response(
        {'detail': 'Only admin/scheduler users can manage the Schedule Build Workspace.'},
        status=status.HTTP_403_FORBIDDEN,
    )


def _schedule_version_queryset(block):
    return (
        ScheduleVersion.objects.filter(schedule_block=block)
        .select_related('domain')
        .prefetch_related('shift_instances', 'optimizer_runs')
    )


def _active_optimizer_run(version):
    return version.optimizer_runs.filter(is_active=True).order_by('-run_number').first()


def _get_optimizer_run_for_version(version, run_id):
    if not run_id:
        return _active_optimizer_run(version)
    try:
        parsed_run_id = int(run_id)
    except (TypeError, ValueError):
        return None
    return OptimizerRun.objects.filter(id=parsed_run_id, schedule_version=version).first()


def _visible_assignment_filter(optimizer_run):
    query = Q(assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL)
    if optimizer_run is not None:
        query |= Q(
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=optimizer_run,
        )
    return query


def _shift_instance_queryset(version, optimizer_run=None):
    return (
        ScheduleShiftInstance.objects.filter(schedule_version=version)
        .filter(
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .select_related('facility', 'shift_template')
        .prefetch_related('assignments__physician__user')
    )


def _facility_timezone(facility):
    try:
        return ZoneInfo(facility.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime_timezone.utc


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_build_context(request, block_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    versions = _schedule_version_queryset(block)
    selected_version = None

    version_id = request.query_params.get('version_id')
    if version_id:
        try:
            version_id = int(version_id)
        except (TypeError, ValueError):
            return Response(
                {'version_id': 'version_id must be a valid integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        selected_version = get_object_or_404(versions, id=version_id)
    else:
        selected_version = versions.filter(status=ScheduleVersion.Status.BUILD).first() or versions.first()

    optimizer_run_id = request.query_params.get('optimizer_run_id')
    if optimizer_run_id and not version_id:
        requested_optimizer_run = OptimizerRun.objects.filter(
            id=optimizer_run_id,
            schedule_version__schedule_block=block,
        ).select_related('schedule_version').first()
        if requested_optimizer_run is not None:
            selected_version = requested_optimizer_run.schedule_version

    selected_optimizer_run = (
        _get_optimizer_run_for_version(selected_version, optimizer_run_id)
        if selected_version
        else None
    )
    optimizer_summary = None
    if selected_optimizer_run:
        optimizer_summary = selected_optimizer_run.optimizer_summary or None
    elif selected_version:
        optimizer_summary = selected_version.optimizer_summary or None
    shift_instances = (
        ScheduleShiftInstanceSerializer(
            _shift_instance_queryset(selected_version, selected_optimizer_run),
            many=True,
            context={'optimizer_run_id': selected_optimizer_run.id if selected_optimizer_run else None},
        ).data
        if selected_version
        else []
    )

    return Response(
        {
            'schedule_block': ScheduleBlockSerializer(block).data,
            'domains': [
                {'id': domain.id, 'name': domain.name}
                for domain in Domain.objects.filter(active=True).order_by('name')
            ],
            'versions': ScheduleVersionSerializer(versions, many=True).data,
            'selected_version': (
                ScheduleVersionSerializer(selected_version).data
                if selected_version
                else None
            ),
            'optimizer_summary': (
                optimizer_summary
            ),
            'optimizer_runs': (
                OptimizerRunSerializer(
                    selected_version.optimizer_runs.order_by('-run_number'),
                    many=True,
                ).data
                if selected_version
                else []
            ),
            'selected_optimizer_run': (
                OptimizerRunSerializer(selected_optimizer_run).data
                if selected_optimizer_run
                else None
            ),
            'shift_instances': shift_instances,
        }
    )


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_schedule_versions(request, block_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    return Response(ScheduleVersionSerializer(_schedule_version_queryset(block), many=True).data)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_shift_instances(request, block_id, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('domain'),
        id=version_id,
        schedule_block=block,
    )
    optimizer_run = _get_optimizer_run_for_version(version, request.query_params.get('optimizer_run_id'))
    return Response(
        ScheduleShiftInstanceSerializer(
            _shift_instance_queryset(version, optimizer_run),
            many=True,
            context={'optimizer_run_id': optimizer_run.id if optimizer_run else None},
        ).data
    )


def _parse_optimizer_seed(request):
    if 'seed' not in request.data or request.data.get('seed') in (None, ''):
        return None, None
    try:
        return int(request.data.get('seed')), None
    except (TypeError, ValueError):
        return None, {'seed': 'seed must be an integer.'}


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_optimize(request, block_id, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    seed, seed_error = _parse_optimizer_seed(request)
    if seed_error:
        return Response(seed_error, status=status.HTTP_400_BAD_REQUEST)
    try:
        summary = optimize_schedule_version(version, created_by=request.user, seed=seed)
    except ValueError as optimizer_error:
        return Response(
            {'detail': str(optimizer_error)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(summary)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_optimizer_runs(request, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    version = get_object_or_404(ScheduleVersion, id=version_id)
    runs = OptimizerRun.objects.filter(schedule_version=version).order_by('-run_number')
    return Response(OptimizerRunSerializer(runs, many=True).data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_run_optimizer(request, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    seed, seed_error = _parse_optimizer_seed(request)
    if seed_error:
        return Response(seed_error, status=status.HTTP_400_BAD_REQUEST)
    try:
        summary = optimize_schedule_version(version, created_by=request.user, seed=seed)
    except ValueError as optimizer_error:
        return Response(
            {'detail': str(optimizer_error)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(summary)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_detail(request, run_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version__schedule_block', 'schedule_version__domain'),
        id=run_id,
    )
    return Response(OptimizerRunSerializer(optimizer_run).data)


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_activate(request, run_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version'),
        id=run_id,
    )
    if optimizer_run.status != OptimizerRun.Status.COMPLETED:
        return Response(
            {'detail': 'Only completed optimizer runs can be activated.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    with transaction.atomic():
        OptimizerRun.objects.filter(
            schedule_version=optimizer_run.schedule_version,
            is_active=True,
        ).exclude(id=optimizer_run.id).update(is_active=False)
        optimizer_run.is_active = True
        optimizer_run.save(update_fields=['is_active'])
        version = optimizer_run.schedule_version
        version.optimizer_summary = optimizer_run.optimizer_summary
        version.save(update_fields=['optimizer_summary', 'updated_at'])
    return Response(OptimizerRunSerializer(optimizer_run).data)


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_violation_report(request, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
    )
    optimizer_run = _get_optimizer_run_for_version(version, request.query_params.get('optimizer_run_id'))
    return Response(build_violation_report(version, optimizer_run=optimizer_run))


@api_view(['GET'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def optimizer_run_violations(request, run_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    optimizer_run = get_object_or_404(
        OptimizerRun.objects.select_related('schedule_version__schedule_block', 'schedule_version__domain'),
        id=run_id,
    )
    return Response(build_violation_report(optimizer_run.schedule_version, optimizer_run=optimizer_run))


def _schedule_version_assignment_summary(version, message, cleared_count=0):
    active_run = _active_optimizer_run(version)
    instances = list(
        ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            date__gte=version.schedule_block.start_date,
            date__lte=version.schedule_block.end_date,
        )
        .prefetch_related('assignments')
    )
    unfilled_shift_count = sum(
        max(instance.required_staffing - instance.assignments.filter(_visible_assignment_filter(active_run)).count(), 0)
        for instance in instances
    )
    return {
        'message': message,
        'assignments_cleared': cleared_count,
        'total_score': 0,
        'unfilled_shift_count': unfilled_shift_count,
        'assignments_made': 0,
        'request_violations_summary': {
            'violations': 0,
            'rewards': 0,
        },
        'rest_violations_blocked': 0,
        'debug': {
            'schedule_version_id': version.id,
            'schedule_block_id': version.schedule_block_id,
            'schedule_block_start_date': version.schedule_block.start_date.isoformat(),
            'schedule_block_end_date': version.schedule_block.end_date.isoformat(),
            'shift_instances_considered': len(instances),
        },
        'workload_summary': [],
    }


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_clear_optimizer_assignments(request, block_id, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Assignments can only be cleared in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        active_run = _active_optimizer_run(version)
        if active_run is None:
            return Response(
                _schedule_version_assignment_summary(
                    version,
                    'No active optimizer run assignments to clear.',
                    0,
                )
            )
        affected_instance_ids = list(
            ScheduleShiftInstance.objects.filter(
                schedule_version=version,
                assignments__assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                assignments__optimizer_run=active_run,
            )
            .distinct()
            .values_list('id', flat=True)
        )
        affected_instances = list(
            ScheduleShiftInstance.objects.select_for_update()
            .filter(id__in=affected_instance_ids)
        )
        cleared_count, _ = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=active_run,
        ).delete()
        for instance in affected_instances:
            _sync_shift_instance_status(instance)
        active_run.is_active = False
        active_run.notes = (active_run.notes + '\n' if active_run.notes else '') + 'Active optimizer assignments were cleared.'
        active_run.save(update_fields=['is_active', 'notes'])
        version.optimizer_summary = {}
        version.save(update_fields=['optimizer_summary', 'updated_at'])

    return Response(
        _schedule_version_assignment_summary(
            version,
            f'Cleared {cleared_count} optimizer-generated assignment(s).',
            cleared_count,
        )
    )


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_version_clear_all_assignments(request, block_id, version_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    version = get_object_or_404(
        ScheduleVersion.objects.select_related('schedule_block', 'domain'),
        id=version_id,
        schedule_block=block,
    )
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Assignments can only be cleared in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        affected_instance_ids = list(
            ScheduleShiftInstance.objects
            .filter(schedule_version=version, assignments__isnull=False)
            .distinct()
            .values_list('id', flat=True)
        )
        affected_instances = list(
            ScheduleShiftInstance.objects.select_for_update()
            .filter(id__in=affected_instance_ids)
        )
        cleared_count, _ = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
        ).delete()
        for instance in affected_instances:
            _sync_shift_instance_status(instance)
        OptimizerRun.objects.filter(schedule_version=version, is_active=True).update(is_active=False)
        version.optimizer_summary = {}
        version.save(update_fields=['optimizer_summary', 'updated_at'])

    return Response(
        _schedule_version_assignment_summary(
            version,
            f'Cleared {cleared_count} assignment(s).',
            cleared_count,
        )
    )


def _sync_shift_instance_status(instance):
    assigned_count = instance.assignments.filter(
        _visible_assignment_filter(_active_optimizer_run(instance.schedule_version))
    ).count()
    next_status = (
        ScheduleShiftInstance.Status.ASSIGNED
        if assigned_count >= instance.required_staffing
        else ScheduleShiftInstance.Status.OPEN
    )
    if instance.status != next_status:
        instance.status = next_status
        instance.save(update_fields=['status', 'updated_at'])


def _format_shift_instance_assignment_label(shift_instance):
    start_label = shift_instance.start_datetime.strftime('%I:%M%p').lstrip('0').lower()
    end_label = shift_instance.end_datetime.strftime('%I:%M%p').lstrip('0').lower()
    start_label = start_label.replace(':00', '')
    end_label = end_label.replace(':00', '')
    start_label = start_label.replace('am', 'a').replace('pm', 'p')
    end_label = end_label.replace('am', 'a').replace('pm', 'p')
    facility_label = shift_instance.facility.short_name or shift_instance.facility.name
    return f'{facility_label} {start_label}-{end_label}'


def _physician_display_name(physician):
    return physician.display_name or physician.user.get_full_name() or physician.user.username


def _overlapping_assignment_for_physician(physician, shift_instance):
    active_run = _active_optimizer_run(shift_instance.schedule_version)
    return (
        ScheduleShiftAssignment.objects.filter(
            _visible_assignment_filter(active_run),
            physician=physician,
            shift_instance__schedule_version=shift_instance.schedule_version,
            shift_instance__start_datetime__lt=shift_instance.end_datetime,
            shift_instance__end_datetime__gt=shift_instance.start_datetime,
        )
        .exclude(shift_instance=shift_instance)
        .select_related(
            'shift_instance__facility',
            'shift_instance__schedule_version',
            'physician__user',
        )
        .order_by('shift_instance__start_datetime', 'shift_instance__id')
        .first()
    )


def _overlapping_assignment_message(physician, overlapping_assignment):
    physician_name = _physician_display_name(physician)
    shift_label = _format_shift_instance_assignment_label(
        overlapping_assignment.shift_instance
    )
    return (
        f'{physician_name} is already assigned to {shift_label}, '
        f'which overlaps this shift.'
    )


def _physician_assignment_eligibility(physician, shift_instance):
    contract_assignment = (
        ContractUserAssignment.objects.filter(
            physician=physician,
            domain=shift_instance.schedule_version.domain,
            contract__active=True,
        )
        .select_related('contract', 'domain')
        .prefetch_related('contract__facilities')
        .first()
    )
    domain_eligible = contract_assignment is not None
    facility_eligible = bool(
        contract_assignment
        and contract_assignment.contract.facilities.filter(
            id=shift_instance.facility_id,
        ).exists()
    )
    can_assign = physician.active and domain_eligible and facility_eligible

    if not physician.active:
        reason = 'Physician is inactive.'
    elif not domain_eligible:
        reason = (
            f'No active Contract assignment in '
            f'{shift_instance.schedule_version.domain.name}.'
        )
    elif not facility_eligible:
        reason = f'Contract does not include {shift_instance.facility.name}.'
    else:
        overlapping_assignment = _overlapping_assignment_for_physician(
            physician,
            shift_instance,
        )
        if overlapping_assignment:
            reason = _overlapping_assignment_message(physician, overlapping_assignment)
            can_assign = False
        else:
            reason = ''

    return {
        'domain_eligible': domain_eligible,
        'facility_eligible': facility_eligible,
        'can_assign': can_assign,
        'ineligibility_reason': reason,
    }


def _assignment_context_payload(shift_instance):
    active_run = _active_optimizer_run(shift_instance.schedule_version)
    shift_instance = (
        ScheduleShiftInstance.objects.select_related(
            'facility',
            'shift_template',
            'schedule_version__domain',
            'schedule_block',
        )
        .prefetch_related('assignments__physician__user')
        .get(id=shift_instance.id)
    )
    assigned_physician_ids = set(
        shift_instance.assignments.filter(
            _visible_assignment_filter(active_run)
        ).values_list('physician_id', flat=True)
    )
    eligible_physicians = []
    for physician in Physician.objects.filter(active=True).select_related('user').order_by(
        'user__last_name',
        'user__first_name',
        'id',
    ):
        display_name = _physician_display_name(physician)
        eligibility = _physician_assignment_eligibility(physician, shift_instance)
        eligible_physicians.append(
            {
                'id': physician.id,
                'name': display_name,
                'already_assigned': physician.id in assigned_physician_ids,
                **eligibility,
            }
        )

    return {
        'shift_instance': ScheduleShiftInstanceSerializer(
            shift_instance,
            context={'optimizer_run_id': active_run.id if active_run else None},
        ).data,
        'eligible_physicians': eligible_physicians,
    }


@api_view(['GET', 'POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_shift_assignments(request, block_id, shift_instance_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    shift_instance = get_object_or_404(
        ScheduleShiftInstance.objects.select_related(
            'facility',
            'schedule_version__domain',
        ),
        id=shift_instance_id,
        schedule_block=block,
    )

    if request.method == 'GET':
        return Response(_assignment_context_payload(shift_instance))

    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or shift_instance.schedule_version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Physicians can only be assigned in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        physician_id = int(request.data.get('physician_id'))
    except (TypeError, ValueError):
        return Response(
            {'physician_id': 'physician_id is required and must be a valid integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    physician = get_object_or_404(
        Physician.objects.select_related('user'),
        id=physician_id,
    )
    if not physician.active:
        return Response(
            {'physician_id': 'Physician is inactive.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    eligibility = _physician_assignment_eligibility(physician, shift_instance)
    if not eligibility['can_assign']:
        return Response(
            {
                'physician_id': eligibility['ineligibility_reason']
                or 'Physician is not eligible for this shift instance.'
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        locked_instance = ScheduleShiftInstance.objects.select_for_update().get(
            id=shift_instance.id
        )
        if ScheduleShiftAssignment.objects.filter(
            shift_instance=locked_instance,
            physician=physician,
        ).exists():
            return Response(
                {'physician_id': 'Physician is already assigned to this shift instance.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        active_run = _active_optimizer_run(locked_instance.schedule_version)
        if locked_instance.assignments.filter(_visible_assignment_filter(active_run)).count() >= locked_instance.required_staffing:
            return Response(
                {'detail': 'This shift instance is already fully staffed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        overlapping_assignment = _overlapping_assignment_for_physician(
            physician,
            locked_instance,
        )
        if overlapping_assignment:
            return Response(
                {'physician_id': _overlapping_assignment_message(
                    physician,
                    overlapping_assignment,
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ScheduleShiftAssignment.objects.create(
            shift_instance=locked_instance,
            physician=physician,
            created_by=request.user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )
        _sync_shift_instance_status(locked_instance)

    return Response(
        _assignment_context_payload(locked_instance),
        status=status.HTTP_201_CREATED,
    )


@api_view(['DELETE'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_shift_assignment_detail(request, block_id, shift_instance_id, assignment_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    shift_instance = get_object_or_404(
        ScheduleShiftInstance.objects.select_related('schedule_version'),
        id=shift_instance_id,
        schedule_block=block,
    )
    if (
        block.build_status != ScheduleBlock.BuildStatus.BUILD
        or shift_instance.schedule_version.status != ScheduleVersion.Status.BUILD
    ):
        return Response(
            {'detail': 'Physicians can only be removed in a BUILD Schedule Version.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    assignment = get_object_or_404(
        ScheduleShiftAssignment,
        id=assignment_id,
        shift_instance=shift_instance,
    )
    assignment.delete()
    _sync_shift_instance_status(shift_instance)
    return Response(_assignment_context_payload(shift_instance))


@api_view(['POST'])
@authentication_classes([CsrfExemptSessionAuthentication])
@permission_classes([IsAuthenticated])
def schedule_block_generate_shift_instances(request, block_id):
    if not _can_manage_build_workspace(request.user):
        return _build_workspace_forbidden_response()

    block = get_object_or_404(ScheduleBlock, id=block_id)
    if block.build_status not in {
        ScheduleBlock.BuildStatus.PRE_BUILD,
        ScheduleBlock.BuildStatus.BUILD,
    }:
        return Response(
            {'detail': 'Shift instances can only be generated for PRE_BUILD or BUILD Schedule Blocks.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        domain_id = int(request.data.get('domain_id'))
    except (TypeError, ValueError):
        return Response(
            {'domain_id': 'domain_id is required and must be a valid integer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    domain = get_object_or_404(Domain, id=domain_id, active=True)

    with transaction.atomic():
        version = (
            ScheduleVersion.objects.select_for_update()
            .filter(
                schedule_block=block,
                domain=domain,
                status=ScheduleVersion.Status.BUILD,
            )
            .order_by('-version_number')
            .first()
        )
        if version is None:
            latest_version_number = (
                ScheduleVersion.objects.filter(schedule_block=block, domain=domain)
                .order_by('-version_number')
                .values_list('version_number', flat=True)
                .first()
                or 0
            )
            version = ScheduleVersion.objects.create(
                schedule_block=block,
                domain=domain,
                version_number=latest_version_number + 1,
                name=f'Build {latest_version_number + 1}',
                status=ScheduleVersion.Status.BUILD,
            )

        templates = list(
            ShiftTemplate.objects.filter(active=True, facility__active=True)
            .select_related('facility')
            .order_by('facility__name', 'start_time', 'id')
        )
        created_count = 0
        current_date = block.start_date
        while current_date <= block.end_date:
            day_name = current_date.strftime('%A')
            for template in templates:
                if day_name not in (template.active_days_of_week or []):
                    continue

                timezone_info = _facility_timezone(template.facility)
                end_date = current_date
                if template.end_time <= template.start_time:
                    end_date = current_date + timedelta(days=1)

                _, created = ScheduleShiftInstance.objects.get_or_create(
                    schedule_version=version,
                    date=current_date,
                    shift_template=template,
                    defaults={
                        'schedule_block': block,
                        'facility': template.facility,
                        'start_datetime': datetime.combine(
                            current_date,
                            template.start_time,
                            tzinfo=timezone_info,
                        ),
                        'end_datetime': datetime.combine(
                            end_date,
                            template.end_time,
                            tzinfo=timezone_info,
                        ),
                        'required_staffing': template.default_staffing_count,
                        'status': ScheduleShiftInstance.Status.OPEN,
                    },
                )
                if created:
                    created_count += 1
            current_date += timedelta(days=1)

        if block.build_status == ScheduleBlock.BuildStatus.PRE_BUILD:
            block.build_status = ScheduleBlock.BuildStatus.BUILD
            block.save(update_fields=['build_status', 'updated_at'])

    total_count = ScheduleShiftInstance.objects.filter(schedule_version=version).count()
    return Response(
        {
            'message': (
                f'Created {created_count} shift instances.'
                if created_count
                else 'Shift instances already exist for this BUILD version.'
            ),
            'created_count': created_count,
            'total_count': total_count,
            'schedule_block': ScheduleBlockSerializer(block).data,
            'schedule_version': ScheduleVersionSerializer(version).data,
        }
    )


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
