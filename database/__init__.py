from .connection import engine, SessionLocal, Base
from . import models

def init_db():
    Base.metadata.create_all(bind=engine)
    from .schema_upgrade import upgrade_schema
    upgrade_schema(engine)
    print("  DB initialized")
