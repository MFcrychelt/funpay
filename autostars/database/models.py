"""AIOSQLite схемы и модели данных."""

CREATE_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    gameau_order_id TEXT,
    username TEXT,
    chat_node TEXT,
    quantity INTEGER DEFAULT 1000,
    price_rub REAL DEFAULT 0.0,
    cost_usdt REAL DEFAULT 0.0,
    cost_rub REAL DEFAULT 0.0,
    profit_rub REAL DEFAULT 0.0,
    status TEXT NOT NULL,
    error TEXT,
    created_ts INTEGER,
    updated_ts INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_ORDERS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""

CREATE_ORDERS_TS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_orders_created_ts ON orders(created_ts);
"""

CREATE_IDEMPOTENCY_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS idempotency_logs (
    idempotency_key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    request_payload TEXT,
    response_payload TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_IDEMPOTENCY_KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# --- Трекинг выполнения задач: журнал жизненного цикла каждого заказа ---
CREATE_TASK_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    ts_epoch INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_TASK_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_task_events_order ON task_events(order_id, ts_epoch);
"""

CREATE_TASK_EVENTS_TS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_task_events_ts ON task_events(ts_epoch);
"""

ALL_SCHEMAS = [
    CREATE_ORDERS_TABLE,
    CREATE_ORDERS_INDEX,
    CREATE_IDEMPOTENCY_LOGS_TABLE,
    CREATE_IDEMPOTENCY_KEYS_TABLE,
    CREATE_SETTINGS_TABLE,
    CREATE_TASK_EVENTS_TABLE,
    CREATE_TASK_EVENTS_INDEX,
    CREATE_TASK_EVENTS_TS_INDEX,
]

# Создаются ПОСЛЕ миграции legacy-баз (нужны колонки, которые миграция добавляет)
POST_MIGRATION_SCHEMAS = [
    CREATE_ORDERS_TS_INDEX,
]

# Колонки, добавляемые миграцией к уже существующей базе orders
MIGRATE_ORDERS_COLUMNS = [
    ("gameau_order_id", "TEXT"),
    ("cost_rub", "REAL DEFAULT 0.0"),
    ("created_ts", "INTEGER"),
    ("updated_ts", "INTEGER"),
]
