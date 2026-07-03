from django.db import models
from apps.facilities.models import Facility
from apps.accounts.models import Physician


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

    def __str__(self):
        return f"{self.name} ({self.facility.name})"

    class Meta:
        ordering = ['facility__name', 'name', 'start_time']
