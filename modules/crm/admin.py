from django.contrib import admin

from .models import Activity, Company, Contact, Deal, Tag


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ["name", "color", "workspace"]
    search_fields = ["name"]


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "domain", "industry", "city", "employees"]
    search_fields = ["name", "domain", "industry"]


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ["full_name", "email", "company", "stage"]
    list_filter = ["stage"]
    search_fields = ["first_name", "last_name", "email"]


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "value", "stage", "expected_close"]
    list_filter = ["stage"]
    search_fields = ["title", "company__name"]


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ["kind", "body", "author", "done", "due_date", "created_at"]
    list_filter = ["kind", "done"]
    search_fields = ["body"]
