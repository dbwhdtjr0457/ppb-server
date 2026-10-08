from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.#-]{1,160}$")]
Count = Annotated[int, Field(strict=True, ge=1, le=1000)]
Counter = Annotated[int, Field(strict=True, ge=0, le=10**15)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credit(StrictModel):
    kind: Literal["report_tokens"]
    # Monotonic total of tokens earned on THIS installation after linking.
    # The collection method is deliberately trusted; this only deduplicates.
    collected_total: Counter


class Packs(StrictModel):
    kind: Literal["buy_packs", "open_packs"]
    set_id: Identifier
    count: Count


class Sale(StrictModel):
    kind: Literal["sell_spares"]
    card_id: Identifier
    count: Count


class BulkSale(StrictModel):
    kind: Literal["sell_bulk"]
    card_ids: Annotated[list[Identifier], Field(min_length=1, max_length=1000)]


class DexClaim(StrictModel):
    kind: Literal["claim_dex"]
    dex_id: Identifier
    step: Annotated[int, Field(strict=True, ge=0, le=1000)] = 0


class GiftClaim(StrictModel):
    kind: Literal["claim_gift"]
    gift_id: Identifier


class Oripa(StrictModel):
    kind: Literal["pull_oripa"]
    envelope: Annotated[int, Field(strict=True, ge=0, le=1000)]


class RefreshOripa(StrictModel):
    kind: Literal["refresh_oripa", "initialize"]


class BonusWindow(StrictModel):
    key: Identifier
    name: Annotated[str, Field(max_length=100)]
    kind: Literal["weekly", "session"]
    utilization: Annotated[float, Field(ge=0, le=10000, allow_inf_nan=False)]
    instance: Annotated[str, Field(max_length=160)]


class Bonus(StrictModel):
    kind: Literal["report_bonus"]
    windows: Annotated[list[BonusWindow], Field(max_length=100)]


class Preferences(StrictModel):
    kind: Literal["set_preferences"]
    opening_mode: Literal["game", "realistic"]
    favorite_card_id: Identifier | None = None
    title: Annotated[int, Field(strict=True, ge=0, le=10000)] | None = None
    # Level titles (native_levels.TITLE_LEVELS). At most one of title/level_title.
    level_title: Annotated[int, Field(strict=True, ge=1, le=100)] | None = None


class LevelClaim(StrictModel):
    kind: Literal["claim_levels"]


class RotationBuy(StrictModel):
    kind: Literal["rotation_buy"]
    card_id: Identifier
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


Command = Annotated[
    Credit
    | Packs
    | Sale
    | BulkSale
    | DexClaim
    | GiftClaim
    | Oripa
    | RefreshOripa
    | Preferences
    | LevelClaim
    | RotationBuy
    | Bonus,
    Field(discriminator="kind"),
]


class CommandRequest(StrictModel):
    request_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    command: Command
    rules_version: Annotated[str, Field(max_length=300)] | None = None
    price_version: Annotated[str, Field(max_length=64)] | None = None
    quoted_tokens: Annotated[int, Field(strict=True, ge=0, le=10**15)] | None = None
