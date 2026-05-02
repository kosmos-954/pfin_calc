# Entry point для Google Cloud Functions.
# Бизнес-логика находится в bot.py.
from bot import handle_request  # noqa: F401  (re-export для GCP)

