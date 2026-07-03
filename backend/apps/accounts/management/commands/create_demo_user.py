from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Create demo user for Atlas application'

    def handle(self, *args, **options):
        # Check if user already exists
        if User.objects.filter(username='ron').exists():
            self.stdout.write(self.style.WARNING('Demo user "ron" already exists'))
            return
        
        # Create the user
        user = User.objects.create_user(
            username='ron',
            password='atlas',
            first_name='Ron',
            last_name='Turner',
            email='ron@atlas.local'
        )
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully created demo user "{user.username}"')
        )
