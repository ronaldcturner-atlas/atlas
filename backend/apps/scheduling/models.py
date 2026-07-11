from django.db import models
from django.contrib.auth.models import User
from apps.facilities.models import Facility
from apps.accounts.models import Physician
from apps.domains.models import Domain


def _format_template_time(time_value):
    hour_24 = time_value.hour
    minute = time_value.minute
    suffix = 'a' if hour_24 < 12 else 'p'
    hour_12 = hour_24 % 12 or 12

    if minute == 0:
        return f"{hour_12}{suffix}"

    return f"{hour_12}:{minute:02d}{suffix}"


class Shift(models.Model):
    """A shift scheduled for a physician at a facility."""

    ROLE_CHOICES = [
        ('physician', 'Physician'),
        ('fast_track', 'Fast Track'),
        ('triage', 'Triage'),
        ('swing', 'Swing'),
        ('night', 'Night'),
        ('backup', 'Backup'),
        ('administrative', 'Administrative'),
    ]

    SHIFT_TYPE_CHOICES = [
        ('clinical', 'Clinical'),
        ('administrative', 'Administrative'),
        ('vacation', 'Vacation'),
        ('cme', 'CME'),
        ('meeting', 'Meeting'),
        ('holiday', 'Holiday'),
    ]

    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('requested', 'Requested'),
        ('approved', 'Approved'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shifts')
    physician = models.ForeignKey(Physician, on_delete=models.CASCADE, related_name='shifts')
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default='physician')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    shift_type = models.CharField(max_length=30, choices=SHIFT_TYPE_CHOICES, default='clinical')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.physician} at {self.facility} - {self.date} {self.start_time}"

    class Meta:
        ordering = ['-date', 'start_time']


class ShiftTemplate(models.Model):
    """A generic recurring shift template not assigned to specific users."""

    DAYS_OF_WEEK = [
        'Sunday',
        'Monday',
        'Tuesday',
        'Wednesday',
        'Thursday',
        'Friday',
        'Saturday',
    ]
    WEEKEND_ALLOWED_DAYS = ['Friday', 'Saturday', 'Sunday']

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shift_templates')
    name = models.CharField(max_length=120)
    start_time = models.TimeField()
    end_time = models.TimeField()
    active_days_of_week = models.JSONField(default=list)
    weekend_days = models.JSONField(default=list)
    night_shift = models.BooleanField(default=False)
    default_staffing_count = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)

    def generated_name(self):
        return f"{self.facility.short_name} {_format_template_time(self.start_time)}-{_format_template_time(self.end_time)}"

    def save(self, *args, **kwargs):
        self.name = self.generated_name()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.generated_name()

    class Meta:
        ordering = ['facility__name', 'name', 'start_time']


class ScheduleBlock(models.Model):
    """Defines the lifecycle and metadata for a schedule planning block."""

    class BuildStatus(models.TextChoices):
        PRE_BUILD = 'PRE_BUILD', 'PRE_BUILD'
        BUILD = 'BUILD', 'BUILD'
        PREVIEW = 'PREVIEW', 'PREVIEW'
        ARCHIVE = 'ARCHIVE', 'ARCHIVE'

    start_date = models.DateField()
    end_date = models.DateField()
    request_open_datetime = models.DateTimeField()
    request_close_datetime = models.DateTimeField()
    build_status = models.CharField(
        max_length=20,
        choices=BuildStatus.choices,
        default=BuildStatus.PRE_BUILD,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.generated_name

    @property
    def generated_name(self):
        start_label = self.start_date.strftime('%b %Y')
        end_label = self.end_date.strftime('%b %Y')
        if start_label == end_label:
            return start_label
        return f'{start_label}-{end_label}'

    class Meta:
        ordering = ['-created_at', '-id']


class ScheduleVersion(models.Model):
    """A domain-scoped schedule workspace version for a Schedule Block."""

    class Status(models.TextChoices):
        BUILD = 'BUILD', 'Build'
        PREVIEW = 'PREVIEW', 'Preview'
        ARCHIVED = 'ARCHIVED', 'Archived'

    schedule_block = models.ForeignKey(
        ScheduleBlock,
        on_delete=models.CASCADE,
        related_name='schedule_versions',
    )
    domain = models.ForeignKey(
        Domain,
        on_delete=models.CASCADE,
        related_name='schedule_versions',
    )
    version_number = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BUILD)
    optimizer_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.schedule_block.generated_name}: {self.name}'

    class Meta:
        ordering = ['-version_number', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['schedule_block', 'domain', 'version_number'],
                name='unique_schedule_version_number_per_block_domain',
            ),
        ]


class OptimizerRun(models.Model):
    """A historical optimizer result for a Schedule Version."""

    class Status(models.TextChoices):
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    schedule_version = models.ForeignKey(
        ScheduleVersion,
        on_delete=models.CASCADE,
        related_name='optimizer_runs',
    )
    run_number = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='created_optimizer_runs',
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    seed = models.BigIntegerField(null=True, blank=True)
    initial_score = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    final_score = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    score_breakdown = models.JSONField(default=dict, blank=True)
    optimizer_summary = models.JSONField(default=dict, blank=True)
    optimizer_debug = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.schedule_version_id}: Run {self.run_number}'

    class Meta:
        ordering = ['-run_number', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['schedule_version', 'run_number'],
                name='unique_optimizer_run_number_per_schedule_version',
            ),
            models.UniqueConstraint(
                fields=['schedule_version'],
                condition=models.Q(is_active=True),
                name='unique_active_optimizer_run_per_schedule_version',
            ),
        ]


class ScheduleShiftInstance(models.Model):
    """A dated shift requirement generated from a recurring Shift Template."""

    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        ASSIGNED = 'ASSIGNED', 'Assigned'

    schedule_version = models.ForeignKey(
        ScheduleVersion,
        on_delete=models.CASCADE,
        related_name='shift_instances',
    )
    schedule_block = models.ForeignKey(
        ScheduleBlock,
        on_delete=models.CASCADE,
        related_name='shift_instances',
    )
    date = models.DateField()
    shift_template = models.ForeignKey(
        ShiftTemplate,
        on_delete=models.PROTECT,
        related_name='shift_instances',
    )
    facility = models.ForeignKey(
        Facility,
        on_delete=models.PROTECT,
        related_name='schedule_shift_instances',
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    required_staffing = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    is_locked_open = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.date}: {self.shift_template.generated_name()}'

    class Meta:
        ordering = ['date', 'start_datetime', 'facility__name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['schedule_version', 'date', 'shift_template'],
                name='unique_shift_template_date_per_schedule_version',
            ),
        ]


class ScheduleShiftAssignment(models.Model):
    """A physician assigned to one dated Schedule Shift Instance."""

    class AssignmentSource(models.TextChoices):
        MANUAL = 'MANUAL', 'Manual'
        OPTIMIZER = 'OPTIMIZER', 'Optimizer'

    shift_instance = models.ForeignKey(
        ScheduleShiftInstance,
        on_delete=models.CASCADE,
        related_name='assignments',
    )
    physician = models.ForeignKey(
        Physician,
        on_delete=models.CASCADE,
        related_name='schedule_shift_assignments',
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='created_schedule_shift_assignments',
        null=True,
        blank=True,
    )
    assignment_source = models.CharField(
        max_length=20,
        choices=AssignmentSource.choices,
        default=AssignmentSource.MANUAL,
    )
    optimizer_run = models.ForeignKey(
        OptimizerRun,
        on_delete=models.SET_NULL,
        related_name='assignments',
        null=True,
        blank=True,
    )
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.shift_instance_id}: {self.physician}'

    class Meta:
        ordering = ['physician__user__last_name', 'physician__user__first_name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['shift_instance', 'physician'],
                condition=models.Q(assignment_source='MANUAL'),
                name='unique_manual_physician_per_schedule_shift_instance',
            ),
            models.UniqueConstraint(
                fields=['shift_instance', 'physician', 'optimizer_run'],
                condition=models.Q(assignment_source='OPTIMIZER', optimizer_run__isnull=False),
                name='unique_optimizer_run_physician_per_shift_instance',
            ),
        ]


class ScheduleRequest(models.Model):
    """A physician request entered for a specific date inside a Schedule Block."""

    class RequestScope(models.TextChoices):
        USER = 'USER', 'User'
        ADMIN = 'ADMIN', 'Admin'

    class RequestType(models.TextChoices):
        DAY_OFF = 'DAY_OFF', 'Day Off'
        SHIFT_OFF = 'SHIFT_OFF', 'Shift Off'
        DAY_ON = 'DAY_ON', 'Day On'
        SHIFT_ON = 'SHIFT_ON', 'Shift On'

    class Weight(models.TextChoices):
        LOW = 'LOW', 'Low'
        MEDIUM = 'MEDIUM', 'Medium'
        HIGH = 'HIGH', 'High'
        FIXED = 'FIXED', 'Fixed'

    schedule_block = models.ForeignKey(
        ScheduleBlock,
        on_delete=models.CASCADE,
        related_name='requests',
    )
    physician = models.ForeignKey(
        Physician,
        on_delete=models.CASCADE,
        related_name='schedule_requests',
    )
    date = models.DateField()
    request_scope = models.CharField(max_length=10, choices=RequestScope.choices, default=RequestScope.USER)
    request_type = models.CharField(max_length=12, choices=RequestType.choices)
    weight = models.CharField(max_length=10, choices=Weight.choices)
    shift_templates = models.ManyToManyField(ShiftTemplate, blank=True, related_name='schedule_requests')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_schedule_requests')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.schedule_block_id}:{self.physician_id}:{self.date}:{self.request_scope}:{self.request_type}'

    class Meta:
        ordering = ['date', 'physician__user__last_name', 'physician__user__first_name', 'request_scope']
        constraints = [
            models.UniqueConstraint(
                fields=['schedule_block', 'physician', 'date'],
                condition=models.Q(request_scope='USER'),
                name='unique_user_request_per_block_physician_date',
            ),
            models.UniqueConstraint(
                fields=['schedule_block', 'physician', 'date'],
                condition=models.Q(request_scope='ADMIN'),
                name='unique_admin_request_per_block_physician_date',
            ),
        ]


class Contract(models.Model):
    """A domain-specific scheduling rule set."""

    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name='contracts')
    name = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    facilities = models.ManyToManyField(Facility, blank=True, related_name='contracts')
    workload_settings = models.JSONField(default=dict, blank=True)
    shift_settings = models.JSONField(default=dict, blank=True)
    night_settings = models.JSONField(default=dict, blank=True)
    weekend_settings = models.JSONField(default=dict, blank=True)
    request_settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.domain.name}: {self.name}'

    class Meta:
        ordering = ['domain__name', 'name', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['domain', 'name'],
                name='unique_contract_name_per_domain',
            ),
        ]


class ContractUserAssignment(models.Model):
    """Assigns a physician to a contract as their default contract for a domain."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='user_assignments')
    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name='contract_user_assignments')
    physician = models.ForeignKey(Physician, on_delete=models.CASCADE, related_name='contract_assignments')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.domain = self.contract.domain
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.physician_id}:{self.domain_id}:{self.contract_id}'

    class Meta:
        ordering = ['physician__user__last_name', 'physician__user__first_name']
        constraints = [
            models.UniqueConstraint(
                fields=['contract', 'physician'],
                name='unique_physician_per_contract_assignment',
            ),
            models.UniqueConstraint(
                fields=['domain', 'physician'],
                name='unique_physician_default_contract_per_domain',
            ),
        ]
