from django.core.management.base import BaseCommand
from analyzer.models import Category, CategoryGroup


DATA = {
    "Essentials": ["Rent", "Groceries", "Utilities", "Transport", "Medical", "Laundry", "Recharge"],
    "Relationships": ["Family Support", "Partner", "Gifts", "Charity", "Festivals"],
    "Growth": ["Books", "Courses", "Certifications", "Gym", "Software Tools", "Career Prep"],
    "Future": ["Insurance", "Investment", "Travel Fund", "Education Fund", "Device Fund"],
    "Enjoyments": ["Dining Out", "Movies", "Gaming", "Shopping", "Subscriptions", "Hobbies"],
    "Savings": ["Emergency Fund", "Savings Transfer", "Recurring Deposit", "Goal Fund"],
    "Income": ["Salary", "Interest", "Refund", "Cashback", "Side Income", "Father Income", "Mother Income", "Friends Loan Received", "Other Income"],
    "Transfers": ["Self Transfer", "Wallet Load", "Credit Card Payment"],
    "Fees & Charges": ["ATM Fee", "Bank Charge", "Penalty"],
    "Liabilities": ["Friends Loan Given", "Friends Loan Repaid", "Borrowing Repayment"],
}


class Command(BaseCommand):
    help = "Seed default category groups and categories"

    def handle(self, *args, **options):
        created_g = created_c = 0
        for group_name, categories in DATA.items():
            group, g_created = CategoryGroup.objects.get_or_create(name=group_name)
            created_g += int(g_created)
            for cat in categories:
                _, c_created = Category.objects.get_or_create(group=group, name=cat)
                created_c += int(c_created)

        self.stdout.write(self.style.SUCCESS(f"Seeded {created_g} groups and {created_c} categories"))