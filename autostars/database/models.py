"""AIOSQLite схемы и модели данных."""

CREATE_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    username TEXT,
    chat_node TEXT,
    quantity INTEGER DEFAULT 1000,
    price_rub REAL DEFAULT 0.0,
    cost_usdt REAL DEFAULT 0.0,
    profit_rub REAL DEFAULT 0.0,
    status TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_ORDERS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
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

ALL_SCHEMAS = [
    CREATE_ORDERS_TABLE,
    CREATE_ORDERS_INDEX,
    CREATE_IDEMPOTENCY_LOGS_TABLE,
    CREATE_IDEMPOTENCY_KEYS_TABLE,
    CREATE_SETTINGS_TABLE,
]
