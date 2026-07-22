from django import forms
from django.contrib.auth import get_user_model

from modules.crm.models import Company, Pipeline

from .models import Workspace


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ex.: V4 Minas, Minha Empresa…"}),
        }


class WhatsAppPromotionForm(forms.Form):
    first_name = forms.CharField(label="Nome", max_length=120)
    last_name = forms.CharField(label="Sobrenome", max_length=120, required=False)
    phone = forms.CharField(label="Telefone", max_length=40)
    email = forms.EmailField(label="E-mail", required=False)
    company = forms.ModelChoiceField(
        label="Empresa", queryset=Company.objects.none(), required=False,
    )
    deal_title = forms.CharField(label="Nome do negócio", max_length=200, required=False)
    value = forms.DecimalField(
        label="Valor", max_digits=12, decimal_places=2, min_value=0,
        required=False, initial=0,
    )
    pipeline = forms.ModelChoiceField(
        label="Pipeline", queryset=Pipeline.objects.none(), required=False,
    )
    stage = forms.CharField(label="Etapa", max_length=40, required=False)
    expected_close = forms.DateField(
        label="Previsão de fechamento",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    owner = forms.ModelChoiceField(
        label="Responsável", queryset=get_user_model().objects.none(), required=False,
    )

    def __init__(self, *args, workspace, mode="contact", **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        self.mode = mode
        self.fields["company"].queryset = Company.objects.filter(workspace=workspace)
        self.fields["pipeline"].queryset = Pipeline.objects.filter(workspace=workspace)
        self.fields["owner"].queryset = get_user_model().objects.filter(
            memberships__workspace=workspace,
        ).distinct()

    def clean(self):
        cleaned = super().clean()
        if self.mode != "contact_deal":
            return cleaned

        for name in ("deal_title", "pipeline", "stage"):
            if not cleaned.get(name):
                self.add_error(name, "Este campo é obrigatório.")
        pipeline = cleaned.get("pipeline")
        stage = cleaned.get("stage")
        if pipeline and stage and not pipeline.stages.filter(key=stage).exists():
            self.add_error("stage", "A etapa escolhida não pertence a esta pipeline.")
        return cleaned
