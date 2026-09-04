from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.models import TimestampMixin, uuid_pk


class _Base(DeclarativeBase):
    pass


class _Widget(_Base, TimestampMixin):
    __tablename__ = "widget"
    widget_id: Mapped[str] = uuid_pk()


def test_uuid_pk_is_a_uuid_primary_key_with_server_default() -> None:
    column = _Widget.__table__.c.widget_id
    assert column.primary_key is True
    assert isinstance(column.type, UUID)
    assert column.server_default is not None


def test_timestamp_mixin_adds_created_at_column() -> None:
    column = _Widget.__table__.c.created_at
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.server_default is not None


def test_timestamp_mixin_field_is_datetime_typed() -> None:
    mapper = _Widget.__mapper__
    assert mapper.attrs["created_at"].class_attribute.type.python_type is datetime
