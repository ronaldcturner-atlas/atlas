from django.db import models


class Facility(models.Model):
    """A medical facility where shifts are scheduled."""
    name = models.CharField(max_length=255)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']
