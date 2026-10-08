from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.game_schemas import Identifier, StrictModel


class Line(StrictModel):
    printing: Identifier
    quantity: Annotated[int, Field(strict=True, ge=1, le=1000)]


class Wish(StrictModel):
    card_id: Identifier
    finish: Identifier | None = None
    target: Annotated[int, Field(strict=True, ge=1, le=1000)] = 1


class OnlineCommand(StrictModel):
    request_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    action: Literal[
        "profile",
        "rotate_code",
        "friend_request",
        "friend_accept",
        "friend_reject",
        "friend_remove",
        "block",
        "unblock",
        "wishlist",
        "binder",
        "trade_create",
        "trade_accept",
        "trade_reject",
        "trade_cancel",
        "trade_counter",
        "listing_create",
        "listing_cancel",
        "listing_buy",
        "notification_read",
    ]
    target_id: UUID | None = None
    target_version: Annotated[int, Field(strict=True, ge=0)] | None = None
    nickname: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    friend_code: Annotated[str, Field(min_length=8, max_length=32)] | None = None
    collection_public: bool = False
    wishlist_public: bool = False
    binder_public: bool = False
    trade_list_public: bool | None = None
    wishes: Annotated[list[Wish], Field(max_length=500)] = []
    binder: Annotated[list[Identifier], Field(max_length=36)] = []
    offered: Annotated[list[Line], Field(max_length=20)] = []
    requested: Annotated[list[Line], Field(max_length=20)] = []
    printing: Identifier | None = None
    quantity: Annotated[int, Field(strict=True, ge=1, le=1000)] = 1
    unit_tokens: Annotated[int, Field(strict=True, ge=1, le=10**12)] | None = None
    notification_id: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def validate_lines(self):
        for lines in (self.offered, self.requested):
            if sum(line.quantity for line in lines) > 1000 or len(
                {line.printing for line in lines}
            ) != len(lines):
                raise ValueError("Each side must have unique printings and at most 1000 cards")
        if set(line.printing for line in self.offered) & set(
            line.printing for line in self.requested
        ):
            raise ValueError("Identical printing cannot be on both sides")
        return self
