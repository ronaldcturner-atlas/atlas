from django.db import models


class Facility(models.Model):
    """A medical facility where shifts are scheduled."""
    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=120)
    timezone = models.CharField(max_length=64, default='UTC')
    color = models.CharField(max_length=7, default='#2563eb')
    active = models.BooleanField(default=True)

    @property
    def display_name(self):
        return self.short_name
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']
