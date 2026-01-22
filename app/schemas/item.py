from datetime import datetime

from pydantic import BaseModel


class ItemCreate(BaseModel):
    name: str


class ItemRead(BaseModel):
    id: int
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}
