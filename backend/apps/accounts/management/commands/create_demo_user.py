from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, User


class Command(BaseCommand):
    help = 'Create demo user for Atlas application'

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            username='ron',
            defaults={
                'first_name': 'Ron',
                'last_name': 'Turner',
                'email': 'ron@atlas.local',
            },
        )
        if created:
            user.set_password('atlas')
            user.save(update_fields=['password'])

        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        user.groups.add(scheduler_group)

        action = 'Created' if created else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(f'{action} demo Scheduler user "{user.username}"')
        )
