import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.paths import DATA_DIR

if os.name == "nt":
    DEFAULT_DATABASE_URL = f"sqlite:///{(DATA_DIR / 'timelapse.db').as_posix()}"
else:
    DEFAULT_DATABASE_URL = "sqlite:///./timelapse.db"

SQLALCHEMY_DATABASE_URL = os.getenv("TIMELAPSE_DATABASE_URL", DEFAULT_DATABASE_URL)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_storage_config_schema():
    inspector = inspect(engine)
    if 'storage_configs' not in inspector.get_table_names():
        Base.metadata.create_all(bind=engine)
        return

    existing_columns = {col['name'] for col in inspector.get_columns('storage_configs')}
    required_columns = {
        'nome': 'VARCHAR',
        'total_bytes': 'INTEGER',
        'usado_bytes': 'INTEGER',
        'livre_bytes': 'INTEGER',
        'percentagem_uso': 'FLOAT',
        'oauth_client_id': 'VARCHAR',
        'oauth_client_secret': 'VARCHAR',
        'oauth_access_token': 'VARCHAR',
        'oauth_refresh_token': 'VARCHAR',
        'oauth_token_expires_at': 'INTEGER',
        'oauth_state': 'VARCHAR',
        'oauth_quota_checked_at': 'INTEGER',
        'storage_quota_checked_at': 'INTEGER',
        'storage_error': 'VARCHAR',
        'backup_last_success_at': 'INTEGER',
        'backup_error': 'VARCHAR',
        'backup_alert_sent_at': 'INTEGER',
    }

    with engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(f'ALTER TABLE storage_configs ADD COLUMN {column_name} {column_type}')
                )


def ensure_alert_schema():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    migrations = {
        'system_settings': {
            'alert_camera_failure': 'BOOLEAN NOT NULL DEFAULT 1',
            'domain_name': "VARCHAR NOT NULL DEFAULT ''",
            'auto_update_enabled': 'BOOLEAN NOT NULL DEFAULT 0',
        },
        'cameras': {
            'last_capture_alert_at': 'INTEGER NOT NULL DEFAULT 0',
        },
    }

    with engine.begin() as connection:
        for table_name, columns in migrations.items():
            if table_name not in tables:
                continue
            existing_columns = {col['name'] for col in inspector.get_columns(table_name)}
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}')
                    )
