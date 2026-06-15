import csv
import hashlib
import io
import json
import os
import random
import re
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
import pikepdf
from django.conf import settings
from google import genai
from google.genai import types

from .models import Category, CategoryRule, Transaction, TransactionStaging


BANK_PREFIX_PATTERNS = [
    r"^UPI(?:/DR|/CR)?/",
    r"^NEFT/",
    r"^IMPS/",
    r"^ACH/",
    r"^TO TRANSFER/",
    r"^BY TRANSFER/",
    r"^MB[:/\- ]",
    r"^POS[:/\- ]",
]

GENERIC_UPI_TOKENS = {
    "UPI", "DR", "CR", "PAY", "PAYMENT", "COLLECT",
    "HDFC", "SBI", "ICICI", "AXIS", "KOTAK", "PNB",
    "YBL", "IBIBO", "OKHDFC", "OKSBI", "OKICICI"
}


def normalize_text(value):
    if isinstance(value, list):
        value = " ".join(str(x) for x in value if x is not None)
    elif value is None:
        value = ""
    else:
        value = str(value)
    return re.sub(r"\s+", " ", value.strip()).upper()


def safe_text(value):
    if isinstance(value, list):
        return " ".join(str(x) for x in value if x is not None).strip()
    if value is None:
        return ""
    return str(value).strip()


def parse_date(value):
    value = safe_text(value)
    if not value:
        return None

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value):
    value = safe_text(value)
    if value in ("", "-", "--", "NA", "N/A"):
        return Decimal("0.00")

    cleaned = (
        value.replace(",", "")
        .replace("₹", "")
        .replace("CR", "")
        .replace("DR", "")
        .replace("Cr", "")
        .replace("Dr", "")
        .strip()
    )

    try:
        return Decimal(cleaned or "0.00")
    except InvalidOperation:
        return Decimal("0.00")


def clean_description(text):
    text = safe_text(text)
    for pattern in BANK_PREFIX_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" /:-")
    return text


def extract_payee(raw_description):
    raw = safe_text(raw_description)
    if not raw:
        return ""

    upper = raw.upper()

    if upper.startswith("UPI/"):
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        filtered = []

        for p in parts:
            pu = p.upper()

            if pu in GENERIC_UPI_TOKENS:
                continue
            if re.fullmatch(r"\d{6,}", p):
                continue
            if re.fullmatch(r"[A-Za-z]{2,}\d{2,}", p):
                continue

            filtered.append(p)

        if filtered:
            return filtered[0]

    cleaned = re.sub(r"\b(UPI|DR|CR)\b", "", raw, flags=re.I).strip(" /-")
    return cleaned[:80]


def make_fingerprint(txn_date, amount, txn_type, raw_description, description, payee, notes=""):
    raw = "|".join([
        str(txn_date or ""),
        str(amount or ""),
        safe_text(txn_type).lower(),
        safe_text(raw_description).lower(),
        safe_text(description).lower(),
        safe_text(payee).lower(),
        safe_text(notes).lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_file_hash(file_field):
    file_field.open("rb")
    h = hashlib.sha256()
    for chunk in file_field.chunks():
        h.update(chunk)
    return h.hexdigest()


def detect_amount_and_type(row):
    raw_description = safe_text(
        row.get("Description")
        or row.get("Narration")
        or row.get("Remarks")
        or row.get("Transaction Details")
        or row.get("Particulars")
        or ""
    )
    raw_upper = normalize_text(raw_description)

    amount_candidates = [
        row.get("Amount"),
        row.get("Txn Amount"),
        row.get("Transaction Amount"),
        row.get("Value"),
        row.get("Withdrawal"),
        row.get("Deposit"),
        row.get("Debit"),
        row.get("Credit"),
        row.get("Dr Amount"),
        row.get("Cr Amount"),
    ]

    amount = Decimal("0.00")
    for candidate in amount_candidates:
        txt = safe_text(candidate)
        if txt not in ("", "0", "0.0", "0.00", "-", "--"):
            amount = abs(parse_amount(txt))
            break

    if "UPI/CR/" in raw_upper or "/CR/" in raw_upper:
        return amount, "credit"

    if "UPI/DR/" in raw_upper or "/DR/" in raw_upper:
        return amount, "debit"

    if "NEFT" in raw_upper and ("CR" in raw_upper or "CREDIT" in raw_upper):
        return amount, "credit"

    if "NEFT" in raw_upper and ("DR" in raw_upper or "DEBIT" in raw_upper):
        return amount, "debit"

    debit_keys = ["Debit", "Withdrawal", "Withdrawals", "Dr Amount", "Debit Amount", "Paid Out"]
    credit_keys = ["Credit", "Deposit", "Deposits", "Cr Amount", "Credit Amount", "Paid In"]

    for key in debit_keys:
        txt = safe_text(row.get(key))
        if txt not in ("", "0", "0.0", "0.00", "-", "--"):
            return abs(parse_amount(txt)), "debit"

    for key in credit_keys:
        txt = safe_text(row.get(key))
        if txt not in ("", "0", "0.0", "0.00", "-", "--"):
            return abs(parse_amount(txt)), "credit"

    if amount > 0:
        return amount, ""

    return Decimal("0.00"), ""


def get_or_create_staging_row(upload, source_type, txn_date, amount, txn_type, raw_description, description, payee, notes, raw_data):
    fingerprint = make_fingerprint(txn_date, amount, txn_type, raw_description, description, payee, notes)
    txn, created = TransactionStaging.objects.get_or_create(
        upload=upload,
        fingerprint=fingerprint,
        defaults={
            "source_type": source_type,
            "txn_date": txn_date,
            "amount": amount,
            "txn_type": txn_type,
            "raw_description": safe_text(raw_description),
            "description": safe_text(description),
            "payee": safe_text(payee),
            "notes": safe_text(notes),
            "raw_data": raw_data,
        },
    )
    return txn, created


def parse_csv_statement(upload):
    upload.file.open("rb")
    raw = upload.file.read().decode("utf-8-sig", errors="ignore")
    stream = io.StringIO(raw, newline="")
    reader = csv.DictReader(stream)
    created_rows = []

    for row in reader:
        raw_description = safe_text(row.get("Description") or row.get("Narration") or row.get("Remarks") or "")
        description = clean_description(raw_description)
        payee = safe_text(row.get("Payee") or row.get("Beneficiary") or extract_payee(raw_description))
        notes = safe_text(row.get("Notes") or row.get("Reference") or row.get("Ref No./Cheque No.") or "")
        txn_date = parse_date(row.get("Date") or row.get("Txn Date") or row.get("Transaction Date"))
        amount, txn_type = detect_amount_and_type(row)

        txn, created = get_or_create_staging_row(
            upload=upload,
            source_type="bank",
            txn_date=txn_date,
            amount=amount,
            txn_type=txn_type,
            raw_description=raw_description,
            description=description,
            payee=payee,
            notes=notes,
            raw_data=row,
        )
        if created:
            created_rows.append(txn)

    return list(upload.staging_transactions.all()) if not created_rows else created_rows


def decrypt_pdf_to_temp(uploaded_file, password):
    uploaded_file.open("rb")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_in:
        temp_in.write(uploaded_file.read())
        input_path = temp_in.name

    output_fd, output_path = tempfile.mkstemp(suffix=".pdf")
    os.close(output_fd)

    with pikepdf.open(input_path, password=password) as pdf:
        pdf.save(output_path)

    return input_path, output_path


def extract_transactions_from_text(text):
    transactions = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    pattern = re.compile(
        r"(\d{2}[\/\-.]\d{2}[\/\-.]\d{4})\s+"
        r"(\d{2}[\/\-.]\d{2}[\/\-.]\d{4})?\s*"
        r"(.+?)\s+"
        r"([\d,]+\.\d{2})\s+"
        r"([\d,]+\.\d{2})$"
    )

    previous_balance = None

    for line in lines:
        match = pattern.search(line)
        if not match:
            continue

        txn_date = parse_date(match.group(1))
        value_date = parse_date(match.group(2)) if match.group(2) else None
        raw_description = safe_text(match.group(3))
        description = clean_description(raw_description)
        amount = abs(parse_amount(match.group(4)))
        balance = abs(parse_amount(match.group(5)))

        raw_upper = raw_description.upper()
        txn_type = ""

        if "UPI/CR/" in raw_upper or "/CR/" in raw_upper:
            txn_type = "credit"
        elif "UPI/DR/" in raw_upper or "/DR/" in raw_upper:
            txn_type = "debit"
        elif " CR " in f" {raw_upper} " or raw_upper.startswith("CR/"):
            txn_type = "credit"
        elif " DR " in f" {raw_upper} " or raw_upper.startswith("DR/"):
            txn_type = "debit"
        elif previous_balance is not None:
            if balance > previous_balance:
                txn_type = "credit"
            elif balance < previous_balance:
                txn_type = "debit"

        transactions.append({
            "txn_date": txn_date,
            "value_date": value_date,
            "raw_description": raw_description,
            "description": description,
            "payee": extract_payee(raw_description),
            "notes": f"Balance: {balance}",
            "amount": amount,
            "txn_type": txn_type,
            "raw_line": line,
        })

        previous_balance = balance

    return transactions


def parse_pdf_statement(upload, password):
    input_path, decrypted_path = decrypt_pdf_to_temp(upload.file, password)
    created_rows = []

    try:
        full_text_parts = []
        with pdfplumber.open(decrypted_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text:
                    full_text_parts.append(text)

        full_text = "\n".join(full_text_parts)
        extracted = extract_transactions_from_text(full_text)

        if not extracted:
            txn, created = get_or_create_staging_row(
                upload=upload,
                source_type="bank",
                txn_date=None,
                amount=Decimal("0.00"),
                txn_type="",
                raw_description="Could not auto-parse PDF transactions",
                description="Could not auto-parse PDF transactions",
                payee="",
                notes=full_text[:5000],
                raw_data={"source": "pdf", "preview": full_text[:1500]},
            )
            if created:
                created_rows.append(txn)
            return list(upload.staging_transactions.all())

        for item in extracted:
            txn, created = get_or_create_staging_row(
                upload=upload,
                source_type="bank",
                txn_date=item["txn_date"],
                amount=item["amount"],
                txn_type=item["txn_type"],
                raw_description=item["raw_description"],
                description=item["description"],
                payee=item["payee"],
                notes=item["notes"],
                raw_data={"source": "pdf", "raw_line": item["raw_line"]},
            )
            if created:
                created_rows.append(txn)

    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass
        try:
            os.remove(decrypted_path)
        except Exception:
            pass

    return list(upload.staging_transactions.all()) if not created_rows else created_rows


def _field_text(txn, applies_to):
    if applies_to == "payee":
        return normalize_text(txn.payee)
    if applies_to == "notes":
        return normalize_text(txn.notes)
    if applies_to == "raw_description":
        return normalize_text(txn.raw_description)
    return normalize_text(txn.description)


def apply_rules(user, staging_rows):
    rules = CategoryRule.objects.filter(user=user, is_active=True).select_related("category")
    unknown = []

    for txn in staging_rows:
        if not txn.txn_type:
            txn.needs_review = True
            txn.save(update_fields=["needs_review"])
            unknown.append(txn)
            continue

        matched = None

        for rule in rules:
            if rule.txn_type and rule.txn_type != txn.txn_type:
                continue

            text = _field_text(txn, rule.applies_to)
            key = normalize_text(rule.keyword)

            if rule.match_type == "exact" and text == key:
                matched = rule.category
                break
            elif rule.match_type == "contains" and key in text:
                matched = rule.category
                break
            elif rule.match_type == "regex":
                try:
                    if re.search(rule.keyword, text, re.I):
                        matched = rule.category
                        break
                except re.error:
                    continue

        if matched:
            txn.rule_category = matched
            txn.final_category = matched
            txn.needs_review = False
        else:
            txn.needs_review = True

        txn.save()

        if not matched:
            unknown.append(txn)

    return unknown


def gemini_client():
    timeout_ms = int(getattr(settings, "GEMINI_TIMEOUT_MS", 120000))
    return genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _build_gemini_prompt(allowed_text, txns_payload):
    return f"""
You are classifying personal finance transactions.

Use the full raw_description as the primary clue.
Use description, payee, notes, amount and txn_type as supporting context.

Choose exactly one category for each transaction from this allowed list only:
{allowed_text}

Return strict JSON array only.
Each item must be:
{{
  "staging_id": integer,
  "category_id": integer,
  "confidence": float,
  "reason": "short reason referencing raw description or payee"
}}

Transactions:
{json.dumps(txns_payload, ensure_ascii=False)}
"""


def _parse_gemini_json(text):
    text = safe_text(text)
    if not text:
        return []

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines.startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return json.loads(text)


def run_ai_categorization(user, unknown_rows, force=False):
    if not getattr(settings, "USE_GEMINI_FALLBACK", False) and not force:
        return {"status": "disabled", "message": "AI fallback is disabled.", "processed": 0}

    if not getattr(settings, "GEMINI_API_KEY", ""):
        return {"status": "disabled", "message": "Gemini API key is missing.", "processed": 0}

    unknown_rows = [row for row in unknown_rows if not row.final_category]
    if not unknown_rows:
        return {"status": "empty", "message": "No unresolved transactions to process.", "processed": 0}

    categories = list(
        Category.objects.filter(is_active=True)
        .select_related("group")
        .values("id", "name", "group__name")
    )
    allowed_text = "\n".join([f'{c["id"]}: {c["group__name"]} > {c["name"]}' for c in categories])

    category_map = {c.id: c for c in Category.objects.all()}
    total_processed = 0
    last_error = None
    batch_size = int(getattr(settings, "GEMINI_BATCH_SIZE", 10))

    for start in range(0, len(unknown_rows), batch_size):
        batch = unknown_rows[start:start + batch_size]
        txn_map = {t.id: t for t in batch}

        txns_payload = []
        for txn in batch:
            txns_payload.append({
                "staging_id": txn.id,
                "raw_description": txn.raw_description,
                "description": txn.description,
                "payee": txn.payee,
                "notes": txn.notes,
                "amount": str(txn.amount),
                "txn_type": txn.txn_type,
                "source_type": txn.source_type,
            })

        prompt = _build_gemini_prompt(allowed_text, txns_payload)

        batch_processed = False

        for attempt in range(3):
            try:
                client = gemini_client()
                response = client.models.generate_content(
                    model=getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash"),
                    contents=prompt,
                )

                predictions = _parse_gemini_json(getattr(response, "text", "") or "")
                processed_this_batch = 0

                for item in predictions:
                    txn = txn_map.get(item.get("staging_id"))
                    category = category_map.get(item.get("category_id"))
                    if txn and category:
                        txn.gemini_category = category
                        txn.final_category = txn.final_category or category
                        txn.gemini_confidence = float(item.get("confidence", 0) or 0)
                        txn.gemini_reason = safe_text(item.get("reason", ""))
                        txn.needs_review = True
                        txn.save()
                        processed_this_batch += 1

                total_processed += processed_this_batch
                batch_processed = True
                break

            except Exception as e:
                last_error = str(e)
                transient = (
                    "503" in last_error
                    or "UNAVAILABLE" in last_error.upper()
                    or "429" in last_error
                    or "TIMEOUT" in last_error.upper()
                    or "DEADLINE" in last_error.upper()
                )
                if transient and attempt < 2:
                    delay = (2 ** attempt) + random.uniform(0, 0.75)
                    time.sleep(delay)
                    continue
                break

        if not batch_processed:
            for txn in batch:
                txn.needs_review = True
                if not txn.gemini_reason:
                    txn.gemini_reason = f"AI unavailable, manual review required. Last error: {last_error}"
                txn.save()

    if total_processed:
        return {
            "status": "success",
            "message": f"AI processed {total_processed} transactions.",
            "processed": total_processed,
        }

    return {
        "status": "failed",
        "message": f"AI could not process now: {last_error}",
        "processed": 0,
    }


def predict_unknown_transactions(user, unknown_rows):
    return run_ai_categorization(user, unknown_rows, force=False)


def learn_rule_from_transaction(user, txn):
    category = txn.final_category or getattr(txn, "category", None)
    if not category:
        return None

    raw_value = txn.payee or txn.description or txn.raw_description or ""

    if isinstance(raw_value, list):
        raw_value = " ".join(str(x) for x in raw_value if x is not None)
    elif raw_value is None:
        raw_value = ""
    else:
        raw_value = str(raw_value)

    source_text = raw_value.strip()
    if not source_text:
        return None

    base_text = source_text
    if "/" in base_text:
        base_text = base_text.split("/")
    if "*" in base_text:
        base_text = base_text.split("*")

    keyword = normalize_text(base_text)
    if not keyword:
        keyword = normalize_text(source_text)

    rule, _ = CategoryRule.objects.get_or_create(
        user=user,
        category=category,
        keyword=keyword,
        applies_to="payee" if txn.payee else "description",
        txn_type=txn.txn_type or "",
        match_type="contains",
        defaults={
            "priority": 100,
            "is_active": True,
            "name": f"Auto rule for {keyword[:40]}",
        },
    )
    return rule


def make_transaction_fingerprint(txn):
    return make_fingerprint(
        txn.txn_date,
        txn.amount,
        txn.txn_type,
        txn.raw_description,
        txn.description,
        txn.payee,
        txn.notes,
    )


def finalize_staging_transactions(user, upload, staging_rows):
    saved = 0
    for txn in staging_rows:
        category = txn.final_category or txn.rule_category or txn.gemini_category
        if not category:
            continue

        fingerprint = txn.fingerprint or make_fingerprint(
            txn.txn_date,
            txn.amount,
            txn.txn_type,
            txn.raw_description,
            txn.description,
            txn.payee,
            txn.notes,
        )

        existing = Transaction.objects.filter(user=user, fingerprint=fingerprint).first()
        if existing:
            existing.upload = upload
            existing.source_type = txn.source_type
            existing.txn_date = txn.txn_date
            existing.amount = txn.amount
            existing.txn_type = txn.txn_type
            existing.raw_description = txn.raw_description
            existing.description = txn.description
            existing.payee = txn.payee
            existing.notes = txn.notes
            existing.category = category
            existing.raw_data = txn.raw_data
            existing.save()
        else:
            Transaction.objects.create(
                user=user,
                upload=upload,
                source_type=txn.source_type,
                txn_date=txn.txn_date,
                amount=txn.amount,
                txn_type=txn.txn_type,
                raw_description=txn.raw_description,
                description=txn.description,
                payee=txn.payee,
                notes=txn.notes,
                fingerprint=fingerprint,
                category=category,
                raw_data=txn.raw_data,
            )
        saved += 1
    return saved