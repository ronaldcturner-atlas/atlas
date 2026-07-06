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
