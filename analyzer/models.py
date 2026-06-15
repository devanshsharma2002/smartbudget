from django.conf import settings
from django.db import models


class CategoryGroup(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    group = models.ForeignKey(CategoryGroup, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("group", "name")
        ordering = ["group__name", "name"]

    def __str__(self):
        return f"{self.group.name} / {self.name}"


class CategoryRule(models.Model):
    MATCH_TYPES = (
        ("exact", "Exact"),
        ("contains", "Contains"),
        ("regex", "Regex"),
    )

    APPLIES_TO_CHOICES = (
        ("raw_description", "Raw description"),
        ("description", "Clean description"),
        ("payee", "Payee"),
        ("notes", "Notes"),
    )

    TXN_TYPE_CHOICES = (
        ("", "Any"),
        ("credit", "Credit"),
        ("debit", "Debit"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="category_rules")
    name = models.CharField(max_length=150, blank=True)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="rules")
    keyword = models.CharField(max_length=255)
    match_type = models.CharField(max_length=20, choices=MATCH_TYPES, default="contains")
    applies_to = models.CharField(max_length=20, choices=APPLIES_TO_CHOICES, default="payee")
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE_CHOICES, blank=True, default="")
    priority = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        label = self.name or self.keyword
        return f"{label} -> {self.category}"


class StatementUpload(models.Model):
    STATUS_CHOICES = (
        ("uploaded", "Uploaded"),
        ("parsed", "Parsed"),
        ("review", "Review"),
        ("finalized", "Finalized"),
        ("failed", "Failed"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="statement_uploads")
    file = models.FileField(upload_to="statements/")
    file_name = models.CharField(max_length=255, blank=True)
    file_hash = models.CharField(max_length=64, blank=True, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="uploaded")
    statement_password_used = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file_name or self.file.name


class TransactionStaging(models.Model):
    SOURCE_CHOICES = (
        ("bank", "Bank"),
        ("cash", "Cash"),
        ("manual", "Manual"),
    )

    upload = models.ForeignKey(StatementUpload, on_delete=models.CASCADE, related_name="staging_transactions")
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="bank")
    txn_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    txn_type = models.CharField(max_length=20, blank=True)
    raw_description = models.TextField(blank=True)
    description = models.TextField(blank=True)
    payee = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    fingerprint = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    rule_category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="rule_staging_transactions"
    )
    gemini_category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="gemini_staging_transactions"
    )
    final_category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="final_staging_transactions"
    )

    gemini_confidence = models.FloatField(null=True, blank=True)
    gemini_reason = models.TextField(blank=True)
    needs_review = models.BooleanField(default=False)
    approved_by_user = models.BooleanField(default=False)
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["txn_date", "id"]

    def chosen_category(self):
        return self.final_category or self.rule_category or self.gemini_category

    def __str__(self):
        return f"{self.txn_date} {self.description[:40]}"


class Transaction(models.Model):
    SOURCE_CHOICES = (
        ("bank", "Bank"),
        ("cash", "Cash"),
        ("manual", "Manual"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions")
    upload = models.ForeignKey(
        StatementUpload, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions"
    )
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="bank")
    txn_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    txn_type = models.CharField(max_length=20, blank=True)
    raw_description = models.TextField(blank=True)
    description = models.TextField(blank=True)
    payee = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    fingerprint = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions")
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-txn_date", "-id"]

    def __str__(self):
        return f"{self.txn_date} - {self.amount}"