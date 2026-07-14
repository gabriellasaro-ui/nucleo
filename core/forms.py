from django import forms

from .models import Workspace


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ex.: V4 Minas, Minha Empresa…"}),
        }
