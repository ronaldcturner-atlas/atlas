from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from apps.facilities.models import Facility


class Physician(models.Model):
    """A physician who can be assigned to shifts."""
    CLINICIAN_TYPE_CHOICES = [
        ('physician', 'Physician'),
        ('pa', 'PA'),
        ('np', 'NP'),
    ]
    ROLE_CHOICES = [
        ('org_admin', 'Org Admin'),
        ('medical_director', 'Medical Director'),
        ('admin', 'Admin'),
        ('staff_physician', 'Staff Physician'),
        ('app', 'APP'),
        ('scheduler', 'Scheduler'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='physician')
    display_name = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=30, blank=True)
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, blank=True)
    primary_facility = models.ForeignKey(
        Facility,
        on_delete=models.SET_NULL,
        related_name='primary_physicians',
        null=True,
        blank=True,
    )
    clinician_type = models.CharField(max_length=20, choices=CLINICIAN_TYPE_CHOICES, default='physician')
    fte = models.DecimalField(max_digits=4, decimal_places=2, default=1.00)
    active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.user.get_full_name() or self.user.username
    
    class Meta:
        ordering = ['user__last_name', 'user__first_name']


class AccountSecurityState(models.Model):
    """Credential state kept separate from scheduling/profile information."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='account_security',
    )
    must_change_password = models.BooleanField(default=False)
    temporary_password_issued_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    def mark_temporary_password_issued(self):
        self.must_change_password = True
        self.temporary_password_issued_at = timezone.now()
        self.save(update_fields=[
            'must_change_password', 'temporary_password_issued_at',
        ])

    def mark_password_changed(self):
        self.must_change_password = False
        self.password_changed_at = timezone.now()
        self.save(update_fields=['must_change_password', 'password_changed_at'])
