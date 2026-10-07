from django.db import models
from django.conf import settings


class Organization(models.Model):
	name = models.CharField(max_length=160, unique=True)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return self.name

	class Meta:
		ordering = ['name']


def get_default_organization_id():
	organization, _ = Organization.objects.get_or_create(
		name='Lowcountry Emergency Physicians',
		defaults={'active': True},
	)
	return organization.id


class Region(models.Model):
	organization = models.ForeignKey(
		Organization,
		on_delete=models.CASCADE,
		related_name='regions',
	)
	name = models.CharField(max_length=160)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return f'{self.organization.name}: {self.name}'

	class Meta:
		ordering = ['organization__name', 'name']
		constraints = [
			models.UniqueConstraint(
				fields=['organization', 'name'],
				name='unique_region_name_per_organization',
			),
		]


def get_default_region_id():
	organization_id = get_default_organization_id()
	region, _ = Region.objects.get_or_create(
		organization_id=organization_id,
		name='Lowcountry Emergency Physicians',
		defaults={'active': True},
	)
	return region.id


class Domain(models.Model):
	region = models.ForeignKey(
		Region,
		on_delete=models.CASCADE,
		related_name='domains',
		default=get_default_region_id,
	)
	name = models.CharField(max_length=120)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self):
		return self.name

	@property
	def organization(self):
		return self.region.organization

	@property
	def organization_id(self):
		return self.region.organization_id

	class Meta:
		ordering = ['name']
		constraints = [
			models.UniqueConstraint(
				fields=['region', 'name'],
				name='unique_domain_name_per_region',
			),
		]


def get_default_domain_id():
	existing_domain = Domain.objects.order_by('-id').first()
	if existing_domain is not None:
		return existing_domain.id
	region_id = get_default_region_id()
	domain, _ = Domain.objects.get_or_create(
		region_id=region_id,
		name='Atlas Default Domain',
		defaults={'active': True},
	)
	return domain.id


class OrganizationMembership(models.Model):
	organization = models.ForeignKey(
		Organization,
		on_delete=models.CASCADE,
		related_name='memberships',
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='organization_memberships',
	)
	is_org_admin = models.BooleanField(default=False)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['user__last_name', 'user__first_name', 'user__username']
		constraints = [
			models.UniqueConstraint(
				fields=['organization', 'user'],
				name='unique_user_membership_per_organization',
			),
		]


class DomainMembership(models.Model):
	class Role(models.TextChoices):
		ORG_ADMIN = 'org_admin', 'Org Admin'
		MEDICAL_DIRECTOR = 'medical_director', 'Medical Director'
		ADMIN = 'admin', 'Admin'
		STAFF_PHYSICIAN = 'staff_physician', 'Staff Physician'
		APP = 'app', 'APP'
		SCHEDULER = 'scheduler', 'Scheduler'
		VIEW_ONLY = 'view_only', 'View Only'
		REGIONAL_ADMIN = 'regional_admin', 'Regional Admin'
		DOMAIN_ADMIN = 'domain_admin', 'Domain Admin'

	domain = models.ForeignKey(
		Domain,
		on_delete=models.CASCADE,
		related_name='memberships',
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='domain_memberships',
	)
	role = models.CharField(max_length=30, choices=Role.choices)
	role_template = models.ForeignKey(
		'RoleTemplate',
		on_delete=models.PROTECT,
		related_name='memberships',
		null=True,
		blank=True,
	)
	clinically_active = models.BooleanField(default=True)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['domain__name', 'user__last_name', 'user__first_name']
		constraints = [
			models.UniqueConstraint(
				fields=['domain', 'user'],
				name='unique_user_membership_per_domain',
			),
		]


class RoleTemplate(models.Model):
	"""Region-scoped, domain-assignable role definition."""

	region = models.ForeignKey(
		Region,
		on_delete=models.CASCADE,
		related_name='role_templates',
	)
	name = models.CharField(max_length=100)
	system_key = models.SlugField(max_length=80, blank=True)
	permissions = models.JSONField(default=list)
	notification_defaults = models.JSONField(default=dict, blank=True)
	active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	last_unassigned_at = models.DateTimeField(null=True, blank=True)

	def __str__(self):
		return f'{self.region.name}: {self.name}'

	class Meta:
		ordering = ['region__name', 'name', 'id']
		constraints = [
			models.UniqueConstraint(
				fields=['region', 'name'],
				name='unique_role_template_name_per_region',
			),
		]


class AuditEvent(models.Model):
	"""Immutable authorization and administrative audit entry."""

	organization = models.ForeignKey(
		Organization,
		on_delete=models.PROTECT,
		related_name='audit_events',
	)
	region = models.ForeignKey(
		Region,
		on_delete=models.PROTECT,
		related_name='audit_events',
		null=True,
		blank=True,
	)
	domain = models.ForeignKey(
		Domain,
		on_delete=models.PROTECT,
		related_name='audit_events',
		null=True,
		blank=True,
	)
	actor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		related_name='atlas_audit_events',
		null=True,
		blank=True,
	)
	target_user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		related_name='atlas_target_audit_events',
		null=True,
		blank=True,
	)
	action = models.CharField(max_length=120)
	details = models.JSONField(default=dict, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ['-created_at', '-id']
