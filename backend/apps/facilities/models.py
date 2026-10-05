from django.db import models
from apps.domains.models import Region, get_default_region_id


class Facility(models.Model):
    """A medical facility where shifts are scheduled."""
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        related_name='facilities',
        default=get_default_region_id,
    )
    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=120)
    timezone = models.CharField(max_length=64, default='UTC')
    color = models.CharField(max_length=7, default='#2563eb')
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)

    @property
    def display_name(self):
        return self.short_name
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['sort_order', 'name', 'id']
