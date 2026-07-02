from django.db import models
from django.contrib.auth.models import User


class Physician(models.Model):
    """A physician who can be assigned to shifts."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='physician')
    
    def __str__(self):
        return self.user.get_full_name() or self.user.username
    
    class Meta:
        ordering = ['user__last_name', 'user__first_name']
