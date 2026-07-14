from django.urls import path

from . import views

app_name = "crm"

urlpatterns = [
    # Companies
    path("companies/", views.company_list, name="company_list"),
    path("companies/rows/", views.company_rows, name="company_rows"),
    path("companies/new/", views.company_form, name="company_create"),
    path("companies/<int:pk>/", views.company_detail, name="company_detail"),
    path("companies/<int:pk>/edit/", views.company_form, name="company_edit"),
    path("companies/<int:pk>/delete/", views.company_delete, name="company_delete"),
    # Contacts
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/rows/", views.contact_rows, name="contact_rows"),
    path("contacts/new/", views.contact_form, name="contact_create"),
    path("contacts/<int:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.contact_form, name="contact_edit"),
    path("contacts/<int:pk>/delete/", views.contact_delete, name="contact_delete"),
    # Deals (kanban)
    path("deals/", views.deal_board, name="deal_board"),
    path("deals/cards/", views.deal_board_cards, name="deal_cards"),
    path("deals/stats/", views.deal_stats, name="deal_stats"),
    path("deals/new/", views.deal_form, name="deal_create"),
    path("deals/<int:pk>/", views.deal_detail, name="deal_detail"),
    path("deals/<int:pk>/edit/", views.deal_form, name="deal_edit"),
    path("deals/<int:pk>/delete/", views.deal_delete, name="deal_delete"),
    path("deals/<int:pk>/move/", views.deal_move, name="deal_move"),
    # Activities (timeline)
    path("activities/new/", views.activity_create, name="activity_create"),
    path("activities/<int:pk>/toggle/", views.activity_toggle, name="activity_toggle"),
    path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),
]
