from django.db import models
from apps.facilities.models import Facility
from apps.accounts.models import Physician


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
        return f"{self.facility.name} {_format_template_time(self.start_time)}-{_format_template_time(self.end_time)}"

    def save(self, *args, **kwargs):
        self.name = self.generated_name()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.generated_name()

    class Meta:
        ordering = ['facility__name', 'name', 'start_time']
