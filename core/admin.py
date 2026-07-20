from django.contrib import admin

from .models import Automation, AutomationRun, CustomField, Event, IntegrationConnection, Membership, Workspace


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ["label", "object_type", "field_type", "workspace", "required"]
    list_filter = ["object_type", "field_type", "workspace"]


@admin.register(Automation)
class AutomationAdmin(admin.ModelAdmin):
    list_display = ["name", "trigger", "action", "active", "run_count", "workspace"]
    list_filter = ["trigger", "action", "active"]


@admin.register(AutomationRun)
class AutomationRunAdmin(admin.ModelAdmin):
    list_display = ["automation", "status", "object_repr", "scheduled_for", "created_at", "workspace"]
    list_filter = ["status", "created_at", "workspace"]
    search_fields = ["automation__name", "object_repr", "summary"]


@admin.register(IntegrationConnection)
class IntegrationConnectionAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "status", "workspace", "updated_at"]
    list_filter = ["provider", "status", "workspace"]
    search_fields = ["name", "workspace__name"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["event_type", "object_repr", "processed", "created_at", "workspace"]
    list_filter = ["event_type", "processed"]


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "workspace", "role"]
    list_filter = ["role", "workspace"]
    search_fields = ["user__username"]
