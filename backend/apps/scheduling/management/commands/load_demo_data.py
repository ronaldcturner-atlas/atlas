from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from apps.facilities.models import Facility
from apps.accounts.models import Physician
from apps.scheduling.models import Shift
from datetime import datetime


class Command(BaseCommand):
    help = 'Load demo data for the calendar'

    def handle(self, *args, **options):
        # Create demo facility
        facility, created = Facility.objects.get_or_create(
            name='Berkeley',
            defaults={}
        )
        self.stdout.write(
            self.style.SUCCESS(f'Facility: {facility.name}')
        )

        # Create demo user and physician
        user, created = User.objects.get_or_create(
            username='demo_physician',
            defaults={
                'first_name': 'John',
                'last_name': 'Doe',
                'email': 'john@example.com'
            }
        )
        
        physician, created = Physician.objects.get_or_create(
            user=user
        )
        self.stdout.write(
            self.style.SUCCESS(f'Physician: {physician}')
        )

        # Create demo shift for November 12, 2026 at 7am-7pm (using timezone-aware datetime)
        shift, created = Shift.objects.get_or_create(
            facility=facility,
            physician=physician,
            start_datetime=timezone.make_aware(datetime(2026, 11, 12, 7, 0, 0)),
            defaults={
                'role': 'Physician',
                'end_datetime': timezone.make_aware(datetime(2026, 11, 12, 19, 0, 0)),
                'status': 'scheduled'
            }
        )
        self.stdout.write(
            self.style.SUCCESS(f'Shift: {shift}')
        )

        self.stdout.write(self.style.SUCCESS('Demo data loaded successfully!'))
