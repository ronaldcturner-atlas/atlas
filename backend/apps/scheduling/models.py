from django.db import models
from apps.facilities.models import Facility
from apps.accounts.models import Physician


class Shift(models.Model):
    """A shift scheduled for a physician at a facility."""
    
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shifts')
    role = models.CharField(max_length=100)
    physician = models.ForeignKey(Physician, on_delete=models.CASCADE, related_name='shifts')
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    
    def __str__(self):
        return f"{self.physician} at {self.facility} - {self.start_datetime}"
    
    class Meta:
        ordering = ['-start_datetime']
