from datetime import datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import MetaData, Numeric, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        datetime: TIMESTAMP(timezone=True),
        Decimal: Numeric(14, 2),
    }


# created_at style column: NOT NULL, defaults to now() in the database
created_at_col = Annotated[datetime, mapped_column(server_default=text("now()"))]
