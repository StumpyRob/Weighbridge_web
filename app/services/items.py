from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item
from ..schemas import ItemCreate


def create_item(db: Session, payload: ItemCreate) -> Item:
    item = Item(name=payload.name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_items(db: Session) -> list[Item]:
    return list(db.scalars(select(Item).order_by(Item.id.desc())))
