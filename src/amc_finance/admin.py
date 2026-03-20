from django.contrib import admin  # pyrefly: ignore
from .models import Account, JournalEntry, LedgerEntry
from .services import get_non_performing_loans, NPL_MIN_BALANCE


class AccountInlineAdmin(admin.TabularInline):
    model = Account


class LedgerEntryInlineAdmin(admin.TabularInline):
    model = LedgerEntry
    readonly_fields = ["account"]


class NPLFilter(admin.SimpleListFilter):
    title = "NPL Status"
    parameter_name = "npl"

    def lookups(self, request, model_admin):
        return [
            ("yes", "NPL"),
            ("no", "Not NPL"),
        ]

    def queryset(self, request, queryset):
        if self.value() not in ("yes", "no"):  # pyrefly: ignore[missing-attribute]
            return queryset

        npl_ids = [a.id for a in get_non_performing_loans()]

        if self.value() == "yes":  # pyrefly: ignore[missing-attribute]
            return queryset.filter(pk__in=npl_ids)

        # "no": loan accounts above threshold that ARE meeting repayment requirements
        return queryset.filter(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character__isnull=False,
            balance__gte=NPL_MIN_BALANCE,
        ).exclude(pk__in=npl_ids)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["id", "account_type", "book", "name", "character", "balance"]
    list_select_related = ["character"]
    search_fields = ["character__name", "name"]
    autocomplete_fields = ["character"]
    list_filter = ["account_type", "book", NPLFilter]


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["id", "date", "description", "creator"]
    list_select_related = ["creator"]
    search_fields = ["creator__name", "description"]
    autocomplete_fields = ["creator"]
    inlines = [LedgerEntryInlineAdmin]


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ["id", "journal_entry", "account", "debit", "credit"]
    list_select_related = ["journal_entry", "account"]
    search_fields = [
        "journal_entry__description",
        "account__name",
        "account__character__name",
    ]
    autocomplete_fields = ["journal_entry", "account"]
