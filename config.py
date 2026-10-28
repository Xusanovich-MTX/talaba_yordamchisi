import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent

# .env faylini yuklash
load_dotenv(BASE_DIR / ".env")

# O'zgaruvchilarni o'qish
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "MTX_2477").lstrip("@")
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "6363461555").split(",")
    if x.strip().isdigit()
}
DB_URL = os.getenv("DB_URL") or "sqlite+aiosqlite:///talaba_bot.db"
HEMIS_SESSION_KEY = os.getenv("HEMIS_SESSION_KEY")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()

# Nisbiy SQLite yo'li ishga tushirish papkasiga bog'liq bo'lmasligi kerak.
db_url = make_url(DB_URL)
if db_url.get_backend_name() == "sqlite" and db_url.database not in (None, "", ":memory:"):
    database_path = Path(db_url.database)
    if not database_path.is_absolute():
        db_url = db_url.set(database=str(BASE_DIR / database_path))
        DB_URL = db_url.render_as_string(hide_password=False)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! .env faylini tekshiring.")
