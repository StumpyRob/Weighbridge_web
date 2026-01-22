from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import ItemCreate, ItemRead
from ..services import items as items_service

router = APIRouter()


@router.post("/", response_model=ItemRead)
def create_item(payload: ItemCreate, db: Session = Depends(get_db)) -> ItemRead:
    return items_service.create_item(db, payload)


@router.get("/", response_model=list[ItemRead])
def list_items(db: Session = Depends(get_db)) -> list[ItemRead]:
    return items_service.list_items(db)
