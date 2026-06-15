import csv
import os
from decimal import Decimal
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView

from .forms import CategoryRuleForm, ExportForm, StatementUploadForm, TransactionForm
from .models import Category, CategoryRule, StatementUpload, Transaction
from .services import (
    apply_rules,
    compute_file_hash,
    finalize_staging_transactions,
    learn_rule_from_transaction,
    make_transaction_fingerprint,
    parse_csv_statement,
    parse_pdf_statement,
    run_ai_categorization,
)


class SignUpView(CreateView):
    form_class = UserCreationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("login")


class AppLoginView(LoginView):
    template_name = "registration/login.html"


@login_required
def dashboard(request):
    qs = Transaction.objects.filter(user=request.user).select_related("category", "category__group")
    today = timezone.localdate()
    month_start = today.replace(day=1)

    income_total = qs.filter(category__group__name="Income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    expense_total = qs.exclude(category__group__name="Income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    month_expense = qs.filter(txn_date__gte=month_start).exclude(category__group__name="Income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    month_income = qs.filter(txn_date__gte=month_start, category__group__name="Income").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    txn_count = qs.count()
    uncategorized_count = qs.filter(category__isnull=True).count()

    biggest_category = (
        qs.exclude(category__isnull=True)
        .values("category__group__name", "category__name")
        .annotate(total_amount=Sum("amount"))
        .order_by("-total_amount")
        .first()
    )

    category_summary = (
        qs.filter(category__isnull=False)
        .values("category__group__name", "category__name")
        .annotate(total_amount=Sum("amount"), txn_count=Count("id"))
        .order_by("-total_amount")[:12]
    )

    top_payees = (
        qs.exclude(payee="")
        .values("payee")
        .annotate(total_amount=Sum("amount"), txn_count=Count("id"))
        .order_by("-total_amount")[:10]
    )

    monthly_trend = (
        qs.annotate(month=TruncMonth("txn_date"))
        .values("month")
        .annotate(total_amount=Sum("amount"), txn_count=Count("id"))
        .order_by("-month")[:6]
    )

    recent_transactions = qs.order_by("-txn_date", "-id")[:15]
    uploads = StatementUpload.objects.filter(user=request.user).order_by("-uploaded_at")[:8]
    rules_count = CategoryRule.objects.filter(user=request.user, is_active=True).count()

    return render(request, "analyzer/dashboard.html", {
        "income_total": income_total,
        "expense_total": expense_total,
        "net_total": income_total - expense_total,
        "month_income": month_income,
        "month_expense": month_expense,
        "txn_count": txn_count,
        "rules_count": rules_count,
        "uncategorized_count": uncategorized_count,
        "biggest_category": biggest_category,
        "category_summary": category_summary,
        "top_payees": top_payees,
        "monthly_trend": monthly_trend,
        "recent_transactions": recent_transactions,
        "uploads": uploads,
    })


@login_required
def upload_statement(request):
    if request.method == "POST":
        form = StatementUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.save(commit=False)
            upload.user = request.user
            upload.status = "uploaded"
            upload.file_name = request.FILES["file"].name
            upload.statement_password_used = bool(form.cleaned_data.get("statement_password"))
            upload.save()

            upload.file_hash = compute_file_hash(upload.file)
            upload.save(update_fields=["file_hash"])

            statement_password = form.cleaned_data.get("statement_password", "")
            ext = Path(upload.file.name).suffix.lower()

            try:
                if ext == ".csv":
                    staging_rows = parse_csv_statement(upload)
                elif ext == ".pdf":
                    if not statement_password:
                        messages.error(request, "Password is required for PDF statements.")
                        upload.delete()
                        return redirect("upload-statement")
                    staging_rows = parse_pdf_statement(upload, statement_password)
                else:
                    messages.error(request, "Only CSV and PDF files are supported right now.")
                    upload.delete()
                    return redirect("upload-statement")

                upload.status = "parsed"
                upload.save(update_fields=["status"])

                unknown = apply_rules(request.user, staging_rows)

                if unknown:
                    upload.status = "review"
                    upload.save(update_fields=["status"])
                    messages.warning(
                        request,
                        f"Upload completed. {len(unknown)} transactions need review. You can process them with AI later or delete this upload."
                    )
                    return redirect("review-upload", upload_id=upload.id)

                finalize_staging_transactions(request.user, upload, staging_rows)
                upload.status = "finalized"
                upload.save(update_fields=["status"])
                messages.success(request, "Statement processed successfully.")
                return redirect("dashboard")

            except Exception as e:
                upload.status = "failed"
                upload.save(update_fields=["status"])
                messages.error(request, f"Upload failed: {str(e)}")
                return redirect("upload-statement")
    else:
        form = StatementUploadForm()

    return render(request, "analyzer/upload.html", {"form": form})


@login_required
def statements_list(request):
    uploads = StatementUpload.objects.filter(user=request.user).order_by("-uploaded_at")
    return render(request, "analyzer/statements_list.html", {"uploads": uploads})


@login_required
def statement_delete(request, pk):
    upload = get_object_or_404(StatementUpload, pk=pk, user=request.user)

    if request.method == "POST":
        related_txns = Transaction.objects.filter(user=request.user, upload=upload)
        txn_count = related_txns.count()

        file_path = None
        try:
            if upload.file:
                file_path = upload.file.path
        except Exception:
            file_path = None

        related_txns.delete()
        upload.staging_transactions.all().delete()
        upload.delete()

        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

        messages.success(request, f"Statement deleted with {txn_count} related saved transactions.")
        return redirect("statements-list")

    return render(request, "analyzer/confirm_delete.html", {
        "object": upload,
        "title": "Delete statement and imported transactions",
    })


@login_required
def review_upload_ai(request, upload_id):
    upload = get_object_or_404(StatementUpload, id=upload_id, user=request.user)
    unresolved = list(upload.staging_transactions.filter(final_category__isnull=True))

    result = run_ai_categorization(request.user, unresolved, force=True)

    if result["status"] == "success":
        messages.success(request, result["message"])
    elif result["status"] == "failed":
        messages.warning(request, result["message"])
    else:
        messages.info(request, result["message"])

    upload.status = "review"
    upload.save(update_fields=["status"])
    return redirect("review-upload", upload_id=upload.id)


@login_required
def review_upload(request, upload_id):
    upload = get_object_or_404(StatementUpload, id=upload_id, user=request.user)

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "process_ai":
            unresolved = list(upload.staging_transactions.filter(final_category__isnull=True))
            result = run_ai_categorization(request.user, unresolved, force=True)

            if result["status"] == "success":
                messages.success(request, result["message"])
            elif result["status"] == "failed":
                messages.warning(request, result["message"])
            else:
                messages.info(request, result["message"])

            return redirect("review-upload", upload_id=upload.id)

        if action == "delete_upload":
            return redirect("statement-delete", pk=upload.id)

        staging_rows = list(
            upload.staging_transactions.select_related(
                "rule_category", "gemini_category", "final_category",
                "rule_category__group", "gemini_category__group", "final_category__group"
            ).all()
        )

        with transaction.atomic():
            for txn in staging_rows:
                selected = request.POST.get(f"category_{txn.id}")
                create_rule = request.POST.get(f"create_rule_{txn.id}") == "on"

                if selected:
                    txn.final_category_id = int(selected)
                    txn.approved_by_user = True
                    txn.needs_review = False
                    txn.save(update_fields=["final_category", "approved_by_user", "needs_review"])

                    if create_rule:
                        learn_rule_from_transaction(request.user, txn)

            finalize_staging_transactions(request.user, upload, staging_rows)
            upload.status = "finalized"
            upload.save(update_fields=["status"])

        messages.success(request, "Transactions approved and saved.")
        return redirect("dashboard")

    staging_rows = upload.staging_transactions.select_related(
        "rule_category", "gemini_category", "final_category",
        "rule_category__group", "gemini_category__group", "final_category__group"
    ).all()

    categories = Category.objects.filter(is_active=True).select_related("group").order_by("group__name", "name")

    return render(request, "analyzer/review.html", {
        "upload": upload,
        "staging_rows": staging_rows,
        "categories": categories,
    })


@login_required
def transactions_list(request):
    qs = Transaction.objects.filter(user=request.user).select_related("category", "category__group").order_by("-txn_date", "-id")

    category_id = request.GET.get("category")
    txn_type = request.GET.get("txn_type")
    source_type = request.GET.get("source_type")

    if category_id:
        qs = qs.filter(category_id=category_id)
    if txn_type:
        qs = qs.filter(txn_type=txn_type)
    if source_type:
        qs = qs.filter(source_type=source_type)

    categories = Category.objects.filter(is_active=True).select_related("group").order_by("group__name", "name")

    return render(request, "analyzer/transactions_list.html", {
        "transactions": qs[:300],
        "categories": categories,
        "selected_category": category_id or "",
        "selected_txn_type": txn_type or "",
        "selected_source_type": source_type or "",
    })


@login_required
def transaction_create(request):
    if request.method == "POST":
        form = TransactionForm(request.POST)
        if form.is_valid():
            txn = form.save(commit=False)
            txn.user = request.user
            txn.fingerprint = make_transaction_fingerprint(txn)
            txn.save()
            messages.success(request, "Transaction added.")
            return redirect("transactions-list")
    else:
        form = TransactionForm(initial={"source_type": "cash"})

    return render(request, "analyzer/transaction_form.html", {"form": form, "title": "Add transaction"})


@login_required
def transaction_update(request, pk):
    txn = get_object_or_404(Transaction, pk=pk, user=request.user)
    if request.method == "POST":
        form = TransactionForm(request.POST, instance=txn)
        if form.is_valid():
            txn = form.save(commit=False)
            txn.fingerprint = make_transaction_fingerprint(txn)
            txn.save()
            messages.success(request, "Transaction updated.")
            return redirect("transactions-list")
    else:
        form = TransactionForm(instance=txn)

    return render(request, "analyzer/transaction_form.html", {"form": form, "title": "Edit transaction"})


@login_required
def transaction_delete(request, pk):
    txn = get_object_or_404(Transaction, pk=pk, user=request.user)
    if request.method == "POST":
        txn.delete()
        messages.success(request, "Transaction deleted.")
        return redirect("transactions-list")
    return render(request, "analyzer/confirm_delete.html", {
        "object": txn,
        "title": "Delete transaction",
    })


@login_required
def create_rule_from_transaction(request, pk):
    txn = get_object_or_404(Transaction, pk=pk, user=request.user)
    category = txn.category
    if not category:
        messages.error(request, "Set a category on the transaction first.")
        return redirect("transactions-list")

    initial = {
        "name": f"Rule for {txn.payee or txn.description[:40]}",
        "category": category,
        "keyword": txn.payee or txn.description or txn.raw_description,
        "match_type": "contains",
        "applies_to": "payee" if txn.payee else "description",
        "txn_type": txn.txn_type,
        "priority": 100,
        "is_active": True,
    }

    if request.method == "POST":
        form = CategoryRuleForm(request.POST)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.user = request.user
            rule.save()
            messages.success(request, "Rule created from transaction.")
            return redirect("rules-list")
    else:
        form = CategoryRuleForm(initial=initial)

    return render(request, "analyzer/rule_form.html", {"form": form, "title": "Create rule from transaction"})


@login_required
def rules_list(request):
    rules = CategoryRule.objects.filter(user=request.user).select_related("category", "category__group").order_by("priority", "id")
    return render(request, "analyzer/rules_list.html", {"rules": rules})


@login_required
def rule_create(request):
    if request.method == "POST":
        form = CategoryRuleForm(request.POST)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.user = request.user
            rule.save()
            messages.success(request, "Rule created.")
            return redirect("rules-list")
    else:
        form = CategoryRuleForm()
    return render(request, "analyzer/rule_form.html", {"form": form, "title": "Add rule"})


@login_required
def rule_update(request, pk):
    rule = get_object_or_404(CategoryRule, pk=pk, user=request.user)
    if request.method == "POST":
        form = CategoryRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Rule updated.")
            return redirect("rules-list")
    else:
        form = CategoryRuleForm(instance=rule)
    return render(request, "analyzer/rule_form.html", {"form": form, "title": "Edit rule"})


@login_required
def rule_delete(request, pk):
    rule = get_object_or_404(CategoryRule, pk=pk, user=request.user)
    if request.method == "POST":
        rule.delete()
        messages.success(request, "Rule deleted.")
        return redirect("rules-list")
    return render(request, "analyzer/confirm_delete.html", {"object": rule, "title": "Delete rule"})


@login_required
def transactions_bulk_delete(request):
    if request.method != "POST":
        messages.error(request, "Invalid request.")
        return redirect("transactions-list")

    selected_ids = request.POST.getlist("selected_transactions")
    if not selected_ids:
        messages.warning(request, "No transactions selected.")
        return redirect("transactions-list")

    qs = Transaction.objects.filter(user=request.user, id__in=selected_ids)
    deleted_count = qs.count()
    qs.delete()

    messages.success(request, f"Deleted {deleted_count} transaction(s).")
    return redirect("transactions-list")


@login_required
def export_csv(request):
    form = ExportForm(request.GET or None)
    rows = None

    if form.is_valid():
        start_date = form.cleaned_data["start_date"]
        end_date = form.cleaned_data["end_date"]
        category = form.cleaned_data.get("category")
        txn_type = form.cleaned_data.get("txn_type")

        qs = Transaction.objects.filter(
            user=request.user,
            txn_date__gte=start_date,
            txn_date__lte=end_date,
        ).select_related("category", "category__group")

        if category:
            qs = qs.filter(category=category)
        if txn_type:
            qs = qs.filter(txn_type=txn_type)

        if request.GET.get("download") == "1":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="transactions_{start_date}_to_{end_date}.csv"'
            writer = csv.writer(response)
            writer.writerow(["Date", "Type", "Source", "Amount", "Raw Description", "Description", "Payee", "Notes", "Category Group", "Category"])
            for txn in qs.order_by("txn_date", "id"):
                writer.writerow([
                    txn.txn_date,
                    txn.txn_type,
                    txn.source_type,
                    txn.amount,
                    txn.raw_description,
                    txn.description,
                    txn.payee,
                    txn.notes,
                    txn.category.group.name if txn.category else "",
                    txn.category.name if txn.category else "",
                ])
            return response

        rows = qs.order_by("-txn_date", "-id")[:150]

    return render(request, "analyzer/export.html", {"form": form, "rows": rows})