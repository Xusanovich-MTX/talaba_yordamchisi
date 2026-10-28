from datetime import datetime, time
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    full_name: Mapped[str] = mapped_column(String(100))
    first_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hemis_institution_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hemis_institution_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    hemis_api_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hemis_student_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hemis_session_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Tashkent")
    notify_class: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_deadline: Mapped[bool] = mapped_column(Boolean, default=True)

    # Bog'lanishlar
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        Index("ix_schedules_user_day_time", "user_id", "day_of_week", "start_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subject: Mapped[str] = mapped_column(String(100))
    day_of_week: Mapped[int] = mapped_column(Integer) # 1=Dushanba, 7=Yakshanba
    start_time: Mapped[time] = mapped_column(Time)
    room_or_link: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Bog'lanish
    user: Mapped["User"] = relationship(back_populates="schedules")

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_user_completed_deadline", "user_id", "is_completed", "deadline"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    subject: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500))
    deadline: Mapped[datetime] = mapped_column(DateTime)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Bog'lanish
    user: Mapped["User"] = relationship(back_populates="tasks")
