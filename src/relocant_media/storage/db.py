from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from relocant_media.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionFactory() as session:
        yield session
