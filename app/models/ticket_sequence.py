from datetime import datetime

from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TicketSequence(Base):
    __tablename__ = "ticket_sequences"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
