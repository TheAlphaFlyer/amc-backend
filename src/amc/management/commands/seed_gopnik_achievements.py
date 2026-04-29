from django.core.management.base import BaseCommand

from amc.models import Guild, GuildAchievement


GOPNIK_ACHIEVEMENTS = [
    {
        "name": "Welcome to Gopnik",
        "description": "Your first zero-comfort delivery.",
        "icon": "🚗",
        "order": 1,
        "criteria": {
            "tracking": "count",
            "goal": 1,
            "log_model": "passenger",
            "comfort": False,
        },
    },
    {
        "name": "Niet Comfort",
        "description": "100 non-comfort deliveries. Embrace the grind.",
        "icon": "😤",
        "order": 2,
        "criteria": {
            "tracking": "count",
            "goal": 100,
            "log_model": "passenger",
            "comfort": False,
        },
    },
    {
        "name": "Zero Stars",
        "description": "Deliver 10 passengers who had comfort=true but comfort_rating=0. The absolute worst.",
        "icon": "⭐",
        "order": 3,
        "criteria": {
            "tracking": "count",
            "goal": 10,
            "log_model": "passenger",
            "comfort": True,
            "max_comfort_rating": 0,
        },
    },
    {
        "name": "Speed Demon",
        "description": "50 urgent deliveries. Safety is optional.",
        "icon": "💨",
        "order": 4,
        "criteria": {
            "tracking": "count",
            "goal": 50,
            "log_model": "passenger",
            "urgent": True,
        },
    },
    {
        "name": "Rough Rider",
        "description": "25 offroad deliveries. Roads are suggestions.",
        "icon": "🏔️",
        "order": 5,
        "criteria": {
            "tracking": "count",
            "goal": 25,
            "log_model": "passenger",
            "offroad": True,
        },
    },
    {
        "name": "VIP Treatment",
        "description": "10 limo deliveries. Luxury without the luxury.",
        "icon": "🥂",
        "order": 6,
        "criteria": {
            "tracking": "count",
            "goal": 10,
            "log_model": "passenger",
            "limo": True,
        },
    },
    {
        "name": "Gopnik Grind",
        "description": "Earn 1,000,000 in guild bonus payments.",
        "icon": "💰",
        "order": 7,
        "criteria": {
            "tracking": "sum_payment",
            "goal": 1_000_000,
            "log_model": "passenger",
        },
    },
    {
        "name": "Full Throttle",
        "description": "200 total deliveries under the Gopnik banner.",
        "icon": "🔥",
        "order": 8,
        "criteria": {
            "tracking": "count",
            "goal": 200,
            "log_model": "passenger",
        },
    },
    {
        "name": "Gopnik Spirit",
        "description": "10 urgent + offroad + non-comfort deliveries. The trifecta of chaos.",
        "icon": "🫡",
        "order": 9,
        "criteria": {
            "tracking": "count",
            "goal": 10,
            "log_model": "passenger",
            "comfort": False,
            "urgent": True,
            "offroad": True,
        },
    },
    {
        "name": "Petty Crime",
        "description": "Your first illicit cargo delivery. Welcome to the dark side.",
        "icon": "🔫",
        "order": 10,
        "criteria": {
            "tracking": "count",
            "goal": 1,
            "log_model": "cargo",
            "is_illicit": True,
        },
    },
    {
        "name": "Career Criminal",
        "description": "50 illicit cargo deliveries.",
        "icon": "🦹",
        "order": 11,
        "criteria": {
            "tracking": "count",
            "goal": 50,
            "log_model": "cargo",
            "is_illicit": True,
        },
    },
    {
        "name": "Money Launderer",
        "description": "Launder 500,000 in dirty money.",
        "icon": "💸",
        "order": 12,
        "criteria": {
            "tracking": "sum_payment",
            "goal": 500_000,
            "log_model": "cargo",
            "cargo_key": "Money",
        },
    },
    {
        "name": "Narcotics Runner",
        "description": "Deliver 25 drug cargos (Ganja, Cocaine, Coca Leaves).",
        "icon": "💊",
        "order": 13,
        "criteria": {
            "tracking": "count",
            "goal": 25,
            "log_model": "cargo",
            "cargo_key_in": ["Ganja", "Cocaine", "CocaLeavesPallet", "GanjaPallet"],
        },
    },
    {
        "name": "Moonshiner",
        "description": "Deliver 10 Moonshine jugs.",
        "icon": "🥃",
        "order": 14,
        "criteria": {
            "tracking": "count",
            "goal": 10,
            "log_model": "cargo",
            "cargo_key": "Moonshine",
        },
    },
    {
        "name": "Dirty Money",
        "description": "Earn 1,000,000 total from illicit cargo.",
        "icon": "🏦",
        "order": 15,
        "criteria": {
            "tracking": "sum_payment",
            "goal": 1_000_000,
            "log_model": "cargo",
            "is_illicit": True,
        },
    },
]


class Command(BaseCommand):
    help = "Seed achievements for the Gopnik Taxi Cooperative guild."

    def add_arguments(self, parser):
        parser.add_argument(
            "--guild",
            default="Gopnik Taxi Cooperative",
            help="Guild name to seed achievements for (default: Gopnik Taxi Cooperative)",
        )

    def handle(self, *args, **options):
        guild_name = options["guild"]
        try:
            guild = Guild.objects.get(name=guild_name)
        except Guild.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'Guild "{guild_name}" does not exist.'))
            return

        created_count = 0
        for data in GOPNIK_ACHIEVEMENTS:
            _, created = GuildAchievement.objects.update_or_create(
                guild=guild,
                name=data["name"],
                defaults={
                    "description": data["description"],
                    "icon": data["icon"],
                    "order": data["order"],
                    "criteria": data["criteria"],
                },
            )
            if created:
                created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {created_count} new achievements for {guild.name} "
                f"({len(GOPNIK_ACHIEVEMENTS)} total)."
            )
        )
