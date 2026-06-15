from django import forms
from .models import Category, CategoryRule, StatementUpload, Transaction


class DateInput(forms.DateInput):
    input_type = "date"


class StatementUploadForm(forms.ModelForm):
    statement_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"placeholder": "PDF password if needed"}),
        help_text="Required only for password-protected PDF statements.",
    )

    class Meta:
        model = StatementUpload
        fields = ["file", "notes", "statement_password"]


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ["txn_date", "txn_type", "amount", "source_type", "raw_description", "description", "payee", "notes", "category"]
        widgets = {
            "txn_date": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "raw_description": forms.Textarea(attrs={"rows": 2}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }


class CategoryRuleForm(forms.ModelForm):
    class Meta:
        model = CategoryRule
        fields = ["name", "category", "keyword", "match_type", "applies_to", "txn_type", "priority", "is_active"]


class ExportForm(forms.Form):
    start_date = forms.DateField(widget=DateInput())
    end_date = forms.DateField(widget=DateInput())
    category = forms.ModelChoiceField(
        queryset=Category.objects.filter(is_active=True),
        required=False,
        empty_label="All categories",
    )
    txn_type = forms.ChoiceField(
        required=False,
        choices=(("", "Any"), ("credit", "Credit"), ("debit", "Debit")),
    )