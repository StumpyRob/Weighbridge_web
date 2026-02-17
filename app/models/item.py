from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..constants import NAME_MAX
from .base import Base, utcnow


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
