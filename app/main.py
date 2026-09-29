from typing import Annotated

from fastapi import Depends, FastAPI, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.schemas import ItemCreate, ItemRead

app = FastAPI(title="ppb-server")

DbSession = Annotated[Session, Depends(get_db)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items", response_model=list[ItemRead])
def list_items(db: DbSession) -> list[Item]:
    return list(db.scalars(select(Item).order_by(Item.id)))


@app.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemCreate, db: DbSession) -> Item:
    item = Item(name=payload.name)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
