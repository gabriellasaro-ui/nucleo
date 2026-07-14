from django import forms

from .models import Activity, Company, Contact, Deal


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "domain", "industry", "employees", "city", "owner", "score", "parent"]


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["first_name", "last_name", "email", "phone", "job_title", "stage", "company", "owner", "score"]


class DealForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = ["title", "value", "stage", "company", "contact", "owner", "expected_close"]
        widgets = {
            "expected_close": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }


class ActivityForm(forms.ModelForm):
    class Meta:
        model = Activity
        fields = ["kind", "body", "due_date"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 2, "placeholder": "Escreva uma nota ou tarefa…"}),
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
