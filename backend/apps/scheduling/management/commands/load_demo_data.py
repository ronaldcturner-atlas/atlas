from calendar import monthrange
from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import Physician
from apps.facilities.models import Facility
from apps.scheduling.models import Shift


class Command(BaseCommand):
    help = 'Load demo data for the calendar'

    def handle(self, *args, **options):
        today = timezone.localdate()
        month_start = date(today.year, today.month, 1)
        _, days_in_month = monthrange(today.year, today.month)

        facilities = [
            'Downtown Medical Center',
            'Riverside Emergency',
            'Northside Hospital',
        ]
        physicians = [
            ('ava', 'Ava', 'Patel', 'ava.patel@example.com'),
            ('ben', 'Ben', 'Carter', 'ben.carter@example.com'),
            ('chloe', 'Chloe', 'Nguyen', 'chloe.nguyen@example.com'),
            ('diego', 'Diego', 'Morales', 'diego.morales@example.com'),
        ]
        shift_templates = [
            ('7a-7p', time(7, 0), time(19, 0)),
            ('7p-7a', time(19, 0), time(7, 0)),
            ('9a-9p', time(9, 0), time(21, 0)),
            ('1p-1a', time(13, 0), time(1, 0)),
            ('Fast Track', time(11, 0), time(21, 0)),
            ('Midday', time(10, 0), time(20, 0)),
        ]

        Shift.objects.filter(
            facility__name='Berkeley',
            physician__user__username='demo_physician',
        ).delete()

        created_facilities = []
        for facility_name in facilities:
            facility, _ = Facility.objects.get_or_create(name=facility_name)
            created_facilities.append(facility)
            self.stdout.write(self.style.SUCCESS(f'Facility: {facility.name}'))

        created_physicians = []
        for username, first_name, last_name, email in physicians:
            user, _ = User.objects.get_or_create(username=username)
            user.first_name = first_name
            user.last_name = last_name
            user.email = email
            user.save(update_fields=['first_name', 'last_name', 'email'])

            physician, _ = Physician.objects.get_or_create(user=user)
            created_physicians.append(physician)
            self.stdout.write(self.style.SUCCESS(f'Physician: {physician}'))

        Shift.objects.filter(
            start_datetime__date__gte=month_start,
            start_datetime__date__lt=month_start + timedelta(days=days_in_month),
        ).delete()

        shifts_created = 0
        for day_index in range(days_in_month):
            shift_date = month_start + timedelta(days=day_index)
            daily_templates = [
                shift_templates[(day_index * 2) % len(shift_templates)],
                shift_templates[(day_index * 2 + 1) % len(shift_templates)],
            ]

            for shift_index, (role, start_time, end_time) in enumerate(daily_templates):
                starts_next_day = end_time <= start_time
                end_date = shift_date + timedelta(days=1 if starts_next_day else 0)

                start_dt = timezone.make_aware(datetime.combine(shift_date, start_time))
                end_dt = timezone.make_aware(datetime.combine(end_date, end_time))

                physician = created_physicians[(day_index * 2 + shift_index) % len(created_physicians)]
                facility = created_facilities[(day_index + shift_index) % len(created_facilities)]

                shift, _ = Shift.objects.get_or_create(
                    facility=facility,
                    physician=physician,
                    start_datetime=start_dt,
                    defaults={
                        'role': role,
                        'end_datetime': end_dt,
                        'status': 'scheduled',
                    },
                )
                shifts_created += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Shift: {shift_date.isoformat()} {role} at {facility.name} for {physician}'
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'Demo data loaded successfully: {len(created_physicians)} physicians, {len(created_facilities)} facilities, {shifts_created} shifts.'
            )
        )
