from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from config import DB_URL
from database.migrations import enable_sqlite_foreign_keys, migrate_database

# Asinxron engine yaratish
engine = create_async_engine(DB_URL, echo=False)
enable_sqlite_foreign_keys(engine)

# Sessiya yaratish (har bir so'rov uchun alohida)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Jadvallarni yaratish, eski ma'lumotlarni ko'chirish va yaxlitlikni tekshirish.
async def init_db():
    return await migrate_database(engine)
