from django.urls import path
from .views import transactions_bulk_delete
from .views import (
    AppLoginView,
    SignUpView,
    create_rule_from_transaction,
    dashboard,
    export_csv,
    review_upload,
    review_upload_ai,
    rule_create,
    rule_delete,
    rule_update,
    rules_list,
    statement_delete,
    statements_list,
    transaction_create,
    transaction_delete,
    transaction_update,
    transactions_list,
    upload_statement,
)

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("dashboard/", dashboard, name="dashboard"),

    path("upload/", upload_statement, name="upload-statement"),
    path("uploads/", statements_list, name="statements-list"),
    path("uploads/<int:pk>/delete/", statement_delete, name="statement-delete"),
    path("review/<int:upload_id>/", review_upload, name="review-upload"),
    path("review/<int:upload_id>/ai/", review_upload_ai, name="review-upload-ai"),

    path("transactions/", transactions_list, name="transactions-list"),
    path("transactions/add/", transaction_create, name="transaction-create"),
    path("transactions/<int:pk>/edit/", transaction_update, name="transaction-update"),
    path("transactions/<int:pk>/delete/", transaction_delete, name="transaction-delete"),
    path("transactions/<int:pk>/create-rule/", create_rule_from_transaction, name="transaction-create-rule"),

    path("rules/", rules_list, name="rules-list"),
    path("rules/add/", rule_create, name="rule-create"),
    path("rules/<int:pk>/edit/", rule_update, name="rule-update"),
    path("rules/<int:pk>/delete/", rule_delete, name="rule-delete"),

    path("export/", export_csv, name="export-csv"),

    path("accounts/signup/", SignUpView.as_view(), name="signup"),
    path("accounts/login/", AppLoginView.as_view(), name="login"),
    path("transactions/bulk-delete/", transactions_bulk_delete, name="transactions-bulk-delete"),
]