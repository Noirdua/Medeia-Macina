from __future__ import annotations

import atexit
import sqlite3
import json
import threading
import os
from queue import Queue
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
import time
import datetime
from SYS.logger import debug, log
import logging
logger = logging.getLogger(__name__)

# DB execute retry settings (for transient 'database is locked' errors)
_DB_EXEC_RETRY_MAX = 5
_DB_EXEC_RETRY_BASE_DELAY = 0.05
_DB_CONNECT_TIMEOUT = 30.0
_DB_BUSY_TIMEOUT_MS = 30000

# The database is located in the project root (prefer explicit repo hints).
def _resolve_root_dir() -> Path:
    env_root = (
        os.environ.get("MM_REPO")
        or os.environ.get("MM_ROOT")
        or os.environ.get("REPO")
    )
    if env_root:
        try:
            candidate = Path(env_root).expanduser().resolve()
            if candidate.exists():
                return candidate
        except Exception as exc:
            logger.debug("_resolve_root_dir: failed to resolve env_root %r: %s", env_root, exc, exc_info=True)

    cwd = Path.cwd().resolve()
    for base in [cwd, *cwd.parents]:
        if (base / "medios.db").exists():
            return base
        if (base / "CLI.py").exists():
            return base
        if (base / "config.conf").exists():
            return base
        if (base / "scripts").exists() and (base / "SYS").exists():
            return base

    return Path(__file__).resolve().parent.parent


ROOT_DIR = _resolve_root_dir()
DB_PATH = (ROOT_DIR / "medios.db").resolve()
LOG_DB_PATH = (ROOT_DIR / "logs.db").resolve()

class Database:
    _instance: Optional[Database] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
            cls._instance._init_db()
        return cls._instance

    def _init_db(self):
        self.db_path = DB_PATH
        db_existed = self.db_path.exists()
        if db_existed:
            debug(f"Opening existing medios.db at {self.db_path}")
        else:
            debug(f"Creating medios.db at {self.db_path}")

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=_DB_CONNECT_TIMEOUT
        )
        self.conn.row_factory = sqlite3.Row
        # Reentrant lock to allow nested DB calls within the same thread (e.g., transaction ->
        # get_config_all / save_config_value) without deadlocking.
        self._conn_lock = threading.RLock()
        self._tx_depth = 0

        # Use WAL mode for better concurrency (allows multiple readers + 1 writer)
        # Set a busy timeout so SQLite waits for short locks rather than immediately failing
        try:
            self.conn.execute(f"PRAGMA busy_timeout = {_DB_BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            logger.warning("Failed to configure SQLite PRAGMAs (busy_timeout/WAL/synchronous): %s", exc)

        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        
        # Config table: stores all settings previously in config.conf
        # category: global, store, provider, tool, networking
        # subtype: e.g., hydrusnetwork, folder, alldebrid
        # item_name: e.g., hn-local, default
        # key: the setting key
        # value: the setting value (serialized to string)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                category TEXT,
                subtype TEXT,
                item_name TEXT,
                key TEXT,
                value TEXT,
                PRIMARY KEY (category, subtype, item_name, key)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config_global (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                document TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config_plugin (
                plugin TEXT NOT NULL,
                instance TEXT NOT NULL,
                document TEXT NOT NULL,
                PRIMARY KEY (plugin, instance)
            )
        """)

        # Logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                level TEXT,
                module TEXT,
                message TEXT
            )
        """)

        # Workers table (for background tasks)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY,
                type TEXT,
                title TEXT,
                description TEXT,
                status TEXT DEFAULT 'running',
                progress REAL DEFAULT 0.0,
                details TEXT,
                result TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Worker stdout/logs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS worker_stdout (
                worker_id TEXT,
                channel TEXT DEFAULT 'stdout',
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(worker_id) REFERENCES workers(id)
            )
        """)
        
        self.conn.commit()

    def get_connection(self):
        return self.conn

    def _rollback_if_autocommit(self) -> None:
        if self._tx_depth != 0:
            return
        try:
            self.conn.rollback()
        except Exception as exc:
            logger.exception("Rollback failed: %s", exc)

    def execute(self, query: str, params: tuple = ()): 
        attempts = 0
        while True:
            retry_delay = 0.0
            # Serialize access to the underlying sqlite connection to avoid
            # concurrent use from multiple threads which can trigger locks.
            with self._conn_lock:
                cursor = self.conn.cursor()
                try:
                    cursor.execute(query, params)
                    if self._tx_depth == 0:
                        self.conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if 'locked' in msg and attempts < _DB_EXEC_RETRY_MAX:
                        attempts += 1
                        retry_delay = _DB_EXEC_RETRY_BASE_DELAY * attempts
                        log(f"Database locked on execute; retry {attempts}/{_DB_EXEC_RETRY_MAX} in {retry_delay:.2f}s")
                        self._rollback_if_autocommit()
                    else:
                        self._rollback_if_autocommit()
                        raise
                except Exception as exc:
                    self._rollback_if_autocommit()
                    logger.exception("Unexpected exception during DB execute: %s", exc)
                    raise
            if retry_delay:
                time.sleep(retry_delay)
                continue

    def executemany(self, query: str, param_list: List[tuple]):
        attempts = 0
        while True:
            retry_delay = 0.0
            with self._conn_lock:
                cursor = self.conn.cursor()
                try:
                    cursor.executemany(query, param_list)
                    if self._tx_depth == 0:
                        self.conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if 'locked' in msg and attempts < _DB_EXEC_RETRY_MAX:
                        attempts += 1
                        retry_delay = _DB_EXEC_RETRY_BASE_DELAY * attempts
                        log(f"Database locked on executemany; retry {attempts}/{_DB_EXEC_RETRY_MAX} in {retry_delay:.2f}s")
                        self._rollback_if_autocommit()
                    else:
                        self._rollback_if_autocommit()
                        raise
                except Exception as exc:
                    self._rollback_if_autocommit()
                    logger.exception("Unexpected exception during DB executemany: %s", exc)
                    raise
            if retry_delay:
                time.sleep(retry_delay)
                continue

    @contextmanager
    def transaction(self):
        """Context manager for a database transaction.

        Transactions acquire the connection lock for the duration of the transaction
        to prevent other threads from performing concurrent operations on the
        same sqlite connection which can lead to locking issues.
        """
        self._conn_lock.acquire()
        nested = self._tx_depth > 0
        if not nested and self.conn.in_transaction:
            try:
                self.conn.commit()
            except Exception:
                self._rollback_if_autocommit()
        self._tx_depth += 1
        try:
            if not nested:
                self.conn.execute("BEGIN")
            try:
                yield self.conn
                if not nested:
                    self.conn.commit()
            except Exception:
                if not nested:
                    try:
                        self.conn.rollback()
                    except Exception:
                        logger.exception("Failed to roll back DB transaction")
                raise
        finally:
            self._tx_depth = max(0, self._tx_depth - 1)
            try:
                self._conn_lock.release()
            except Exception:
                logger.exception("Failed to release DB connection lock")

    def fetchall(self, query: str, params: tuple = ()):
        with self._conn_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(query, params)
                return cursor.fetchall()
            finally:
                cursor.close()

    def fetchone(self, query: str, params: tuple = ()): 
        with self._conn_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(query, params)
                return cursor.fetchone()
            finally:
                cursor.close()
class _LazyDatabase:
    """Open SQLite on first use instead of at import."""

    def __init__(self) -> None:
        self._instance: Optional[Database] = None

    def _db(self) -> Database:
        instance = self._instance
        if instance is None:
            instance = Database()
            self._instance = instance
        return instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db(), name)


db = _LazyDatabase()

_LOG_QUEUE: Queue = Queue()
_LOG_THREAD_STARTED = False
_LOG_THREAD_LOCK = threading.Lock()
_LOG_WRITE_COUNT = 0
_LOG_PRUNE_INTERVAL = 500   # prune every N successful writes
_LOG_MAX_AGE_DAYS = 7       # keep logs for this many days
_LOG_WRITE_RETRY_MAX = 3
_LOG_WRITE_RETRY_BASE_DELAY = 0.05
_LOG_SENTINEL = object()
_LOG_THREAD_REF: Optional[threading.Thread] = None


def _ensure_log_db_schema() -> None:
    try:
        conn = sqlite3.connect(
            str(LOG_DB_PATH),
            timeout=_DB_CONNECT_TIMEOUT,
            check_same_thread=False,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {_DB_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    level TEXT,
                    module TEXT,
                    message TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _log_worker_loop() -> None:
    """Background log writer using a temporary per-write connection with
    small retry/backoff and a file fallback when writes fail repeatedly.
    """
    _ensure_log_db_schema()
    global _LOG_WRITE_COUNT
    while True:
        item = _LOG_QUEUE.get()
        if item is _LOG_SENTINEL:
            break
        level, module, message = item
        try:
            attempts = 0
            written = False
            while attempts < _LOG_WRITE_RETRY_MAX and not written:
                conn = None
                cur = None
                try:
                    conn = sqlite3.connect(str(LOG_DB_PATH), timeout=_DB_CONNECT_TIMEOUT)
                    try:
                        conn.execute(f"PRAGMA busy_timeout = {_DB_BUSY_TIMEOUT_MS}")
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA synchronous=NORMAL")
                    except sqlite3.Error:
                        pass
                    cur = conn.cursor()
                    cur.execute("INSERT INTO logs (level, module, message) VALUES (?, ?, ?)", (level, module, message))
                    conn.commit()
                    _LOG_WRITE_COUNT += 1
                    if _LOG_WRITE_COUNT % _LOG_PRUNE_INTERVAL == 0:
                        try:
                            cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=_LOG_MAX_AGE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                            cur.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
                            conn.commit()
                        except Exception:
                            pass
                    written = True
                except sqlite3.OperationalError as exc:
                    attempts += 1
                    if 'locked' in str(exc).lower():
                        time.sleep(_LOG_WRITE_RETRY_BASE_DELAY * attempts)
                        continue
                    # Non-lock operational errors: abort attempts
                    log(f"Warning: Failed to write log entry (operational): {exc}")
                    break
                except Exception as exc:
                    log(f"Warning: Failed to write log entry: {exc}")
                    break
                finally:
                    if cur is not None:
                        try:
                            cur.close()
                        except Exception:
                            pass
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            if not written:
                # Fallback to a file-based log so we never lose the message silently
                try:
                    fallback_dir = Path(db.db_path).with_name("logs")
                    fallback_dir.mkdir(parents=True, exist_ok=True)
                    fallback_file = fallback_dir / "log_fallback.txt"
                    with fallback_file.open("a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.datetime.utcnow().isoformat()}Z [{level}] {module}: {message}\n")
                except Exception as exc:
                    # Last resort: print to stderr
                    try:
                        log(f"ERROR: Could not persist log message: {level} {module} {message}")
                    except Exception:
                        pass
                    try:
                        import sys as _sys, traceback as _tb
                        _sys.stderr.write(f"CRITICAL: Could not persist log message to fallback file: {exc}\n")
                        _tb.print_exc(file=_sys.stderr)
                    except Exception:
                        pass
        finally:
            try:
                _LOG_QUEUE.task_done()
            except Exception as exc:
                try:
                    import sys as _sys, traceback as _tb
                    _sys.stderr.write(f"CRITICAL: Failed to mark log task done: {exc}\n")
                    _tb.print_exc(file=_sys.stderr)
                except Exception:
                    pass


def _ensure_log_thread() -> None:
    global _LOG_THREAD_STARTED, _LOG_THREAD_REF
    if _LOG_THREAD_STARTED:
        return
    with _LOG_THREAD_LOCK:
        if _LOG_THREAD_STARTED:
            return
        thread = threading.Thread(
            target=_log_worker_loop,
            name="mediosdb-log",
            daemon=True
        )
        thread.start()
        _LOG_THREAD_REF = thread
        _LOG_THREAD_STARTED = True


def _shutdown_log_thread() -> None:
    global _LOG_THREAD_REF
    _LOG_QUEUE.put(_LOG_SENTINEL)
    thread = _LOG_THREAD_REF
    if thread is not None:
        try:
            thread.join(timeout=3.0)
        except Exception:
            pass
        _LOG_THREAD_REF = None


atexit.register(_shutdown_log_thread)

def get_db() -> Database:
    return db

def log_to_db(level: str, module: str, message: str):
    """Log a message to the database asynchronously."""
    try:
        _ensure_log_thread()
        _LOG_QUEUE.put((level, module, message))
    except Exception:
        # Avoid recursive logging errors if the queue fails
        pass

# Initialize DB logger in the unified logger
try:
    from SYS.logger import set_db_logger
    set_db_logger(log_to_db)
except ImportError:
    pass

def save_config_value(category: str, subtype: str, item_name: str, key: str, value: Any):
    """Update one config field in the document store and invalidate the cache."""
    config = get_config_all()
    if category == "global":
        config[key] = value
    elif category == "plugin":
        plugins = config.setdefault("plugin", {})
        block = plugins.setdefault(subtype, {})
        instance = str(item_name or "default")
        if instance.lower() == "default" and not isinstance(next(iter(block.values()), None), dict):
            block[key] = value
        else:
            settings = block.setdefault(instance, {})
            if isinstance(settings, dict):
                settings[key] = value
    else:
        return
    with db.transaction() as conn:
        write_config_documents(conn, config)
    try:
        from SYS.config import clear_config_cache
        clear_config_cache()
    except Exception:
        pass

def rows_to_config(rows) -> Dict[str, Any]:
    """Convert DB rows (category, subtype, item_name, key, value) into a config dict.

    This central helper is used by `get_config_all` and callers that need to
    parse rows fetched with a transaction connection to avoid nested lock
    acquisitions.
    """
    config: Dict[str, Any] = {}
    for row in rows:
        cat = row['category']
        sub = row['subtype']
        name = row['item_name']
        key = row['key']
        val = row['value']

        # Drop legacy folder store entries (folder store is removed).
        if cat == 'store' and str(sub).strip().lower() == 'folder':
            continue

        # JSON only. Values that are not JSON stay raw strings.
        # Decrypt encrypted values before use.
        try:
            from SYS.config_crypto import decrypt_config_value
            val = decrypt_config_value(str(val or ""), cat, sub, key)
        except Exception:
            pass
        parsed_val = val
        try:
            if isinstance(val, str):
                s = val.strip()
                if s == "":
                    parsed_val = ""
                else:
                    first = s[0]
                    lowered = s.lower()
                    if first in ('{', '[', '"') or lowered in ('true', 'false', 'null') or __import__('re').fullmatch(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', s):
                        try:
                            parsed_val = json.loads(val)
                        except Exception:
                            parsed_val = val
                    else:
                        parsed_val = val
            else:
                try:
                    parsed_val = json.loads(val)
                except Exception:
                    # Non-string values can sometimes be bytes or Python literals; try decoding when appropriate
                    try:
                        if isinstance(val, (bytes, bytearray)):
                            parsed_val = json.loads(val.decode('utf-8', errors='replace'))
                        else:
                            parsed_val = val
                    except Exception:
                        parsed_val = val
        except Exception as exc:
            logger.debug("rows_to_config: failed to parse value for key %s; using raw value", key, exc_info=True)
            parsed_val = val

        if cat == 'global':
            config[key] = parsed_val
        else:
            # Modular structure: config[category][subtype][item_name?][key]
            if cat == 'plugin':
                cat_dict = config.setdefault('plugin', {})
                sub_dict = cat_dict.setdefault(sub, {})
                if str(name or '').strip().lower() == 'default':
                    sub_dict[key] = parsed_val
                else:
                    name_dict = sub_dict.setdefault(name, {})
                    name_dict[key] = parsed_val
            elif cat in ('provider', 'store', 'tool'):
                config.setdefault(cat, {}).setdefault(sub, {})[key] = parsed_val
            else:
                config.setdefault(cat, {})[key] = parsed_val

    return config


def _decode_config_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    first = text[0]
    lowered = text.lower()
    if first in ("{", "[", '"') or lowered in ("true", "false", "null"):
        try:
            return json.loads(value)
        except Exception:
            return value
    if __import__("re").fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except Exception:
            return value
    if __import__("re").fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except Exception:
            return value
    return value


def _plugin_settings_from_document(plugin: str, document: Any) -> Dict[str, Any]:
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except Exception:
            return {}
    if not isinstance(document, dict):
        return {}
    settings: Dict[str, Any] = {}
    for key, value in document.items():
        key_text = str(key)
        secret = False
        try:
            from SYS.config_crypto import decrypt_config_value, is_secret_key

            secret = bool(is_secret_key(plugin, key_text))
            if isinstance(value, str):
                value = decrypt_config_value(value, "plugin", plugin, key_text)
        except Exception:
            secret = False
        if isinstance(value, str) and not secret:
            value = _decode_config_scalar(value)
        elif isinstance(value, str) and secret and value[:1] in "{[\"":
            value = _decode_config_scalar(value)
        settings[key_text] = value
    return settings


def read_config_documents(conn) -> Dict[str, Any]:
    """Load the in-memory config dict from JSON documents."""
    global_doc: Dict[str, Any] = {}
    cur = conn.cursor()
    try:
        cur.execute("SELECT document FROM config_global WHERE id = 1")
        row = cur.fetchone()
        if row and row[0]:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                global_doc = parsed
        cur.execute("SELECT plugin, instance, document FROM config_plugin")
        plugin_rows = cur.fetchall()
    finally:
        cur.close()

    config = dict(global_doc)
    plugins: Dict[str, Any] = {}
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for plugin, instance, document in plugin_rows:
        plugin_name = str(plugin or "").strip()
        instance_name = str(instance or "").strip() or "default"
        if not plugin_name:
            continue
        grouped.setdefault(plugin_name, {})[instance_name] = _plugin_settings_from_document(
            plugin_name, document
        )
    multi_names: set[str] = set()
    try:
        from SYS.config import _multi_instance_plugin_names

        multi_names = {str(name).strip().lower() for name in _multi_instance_plugin_names()}
    except Exception:
        multi_names = set()
    for plugin_name, instances in grouped.items():
        named = {name for name in instances if name.lower() != "default"}
        if plugin_name.strip().lower() in multi_names or named:
            plugins[plugin_name] = instances
        else:
            plugins[plugin_name] = instances.get("default", next(iter(instances.values()), {}))
    if plugins:
        config["plugin"] = plugins
    return config


def write_config_documents(conn, config: Dict[str, Any]) -> int:
    """Replace stored documents from an in-memory config dict. Returns row count."""
    from SYS.config import (
        _is_multi_instance_plugin_config,
        _multi_instance_plugin_names,
        normalize_multi_instance_plugin_block,
    )

    try:
        from SYS.config_crypto import encrypt_config_value
    except Exception:
        encrypt_config_value = None

    global_doc = {
        key: value
        for key, value in config.items()
        if key not in {"plugin", "provider", "store", "tool"}
        and not str(key).startswith("_")
        and value is not None
    }
    plugin_rows: List[tuple] = []
    plugins = config.get("plugin") if isinstance(config.get("plugin"), dict) else {}
    multi_names = {str(name).strip().lower() for name in _multi_instance_plugin_names()}
    for subtype, instances in plugins.items():
        if not isinstance(instances, dict):
            continue
        plugin_name = str(subtype or "").strip()
        if not plugin_name:
            continue
        force_multi = plugin_name.lower() in multi_names
        write_block = instances
        if force_multi:
            write_block = normalize_multi_instance_plugin_block(plugin_name, instances)
        if force_multi or _is_multi_instance_plugin_config(write_block):
            instance_items = write_block.items()
        else:
            instance_items = (("default", write_block),)
        for instance_name, settings in instance_items:
            if not isinstance(settings, dict):
                continue
            stored: Dict[str, Any] = {}
            secret = None
            try:
                from SYS.config_crypto import is_secret_key

                secret = is_secret_key
            except Exception:
                secret = None
            for key, value in settings.items():
                key_text = str(key)
                if secret is not None and secret(plugin_name, key_text) and encrypt_config_value is not None:
                    stored[key_text] = encrypt_config_value(
                        config, "plugin", plugin_name, key_text, value
                    )
                else:
                    stored[key_text] = value
            plugin_rows.append(
                (plugin_name, str(instance_name), json.dumps(stored, ensure_ascii=False))
            )

    conn.execute("DELETE FROM config_global")
    conn.execute("DELETE FROM config_plugin")
    conn.execute(
        "INSERT INTO config_global (id, document) VALUES (1, ?)",
        (json.dumps(global_doc, ensure_ascii=False),),
    )
    count = 1
    for plugin_name, instance_name, document in plugin_rows:
        conn.execute(
            "INSERT INTO config_plugin (plugin, instance, document) VALUES (?, ?, ?)",
            (plugin_name, instance_name, document),
        )
        count += 1
    return count


def _fold_legacy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Move pre-plugin store/provider/tool blocks into plugin documents."""
    plugins = config.get("plugin")
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugin"] = plugins
    legacy = {}
    for section_name in ("store", "provider", "tool"):
        section = config.pop(section_name, None)
        if isinstance(section, dict):
            legacy[section_name] = section
    debrid = None
    store = legacy.get("store") or {}
    provider = legacy.get("provider") or {}
    if isinstance(store.get("debrid"), dict):
        debrid = store.get("debrid")
    elif isinstance(provider.get("alldebrid"), dict):
        debrid = provider.get("alldebrid")
    if isinstance(debrid, dict) and debrid:
        target = plugins.get("alldebrid")
        if not isinstance(target, dict):
            target = {}
        for key, value in debrid.items():
            target.setdefault(key, value)
        plugins["alldebrid"] = target
    for section in legacy.values():
        for name, block in section.items():
            if str(name) in {"debrid", "alldebrid"}:
                continue
            if isinstance(block, dict) and str(name) not in plugins:
                plugins[str(name)] = block
    if not plugins:
        config.pop("plugin", None)
    return config


def _documents_initialized(conn) -> bool:
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM config_global LIMIT 1")
        if cur.fetchone():
            return True
        cur.execute("SELECT 1 FROM config_plugin LIMIT 1")
        return cur.fetchone() is not None
    finally:
        cur.close()


def get_config_all() -> Dict[str, Any]:
    """Retrieve configuration as JSON documents, migrating the old EAV table once."""
    with db.transaction() as conn:
        if not _documents_initialized(conn):
            cur = conn.cursor()
            try:
                cur.execute("SELECT category, subtype, item_name, key, value FROM config")
                rows = cur.fetchall()
            except Exception:
                rows = []
            finally:
                cur.close()
            if rows:
                migrated = _fold_legacy_config(rows_to_config(rows))
                write_config_documents(conn, migrated)
                cur = conn.cursor()
                try:
                    cur.execute("DELETE FROM config")
                finally:
                    cur.close()
        return read_config_documents(conn)


# Worker Management Methods for medios.db

_WORKER_DB_DEFAULT_TIMEOUT = 0.75
_WORKER_DB_DEFAULT_RETRIES = 1
_WORKER_DB_RETRY_BASE_DELAY = 0.05

def _worker_db_connect(timeout: float = _WORKER_DB_DEFAULT_TIMEOUT) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=timeout,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        busy_ms = max(1, int(timeout * 1000))
        conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return conn


def _worker_db_execute(
    query: str,
    params: tuple = (),
    *,
    fetch: Optional[str] = None,
    timeout: float = _WORKER_DB_DEFAULT_TIMEOUT,
    retries: int = _WORKER_DB_DEFAULT_RETRIES,
) -> Any:
    attempts = 0
    while True:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _worker_db_connect(timeout=timeout)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                if fetch == "one":
                    result = cursor.fetchone()
                elif fetch == "all":
                    result = cursor.fetchall()
                else:
                    result = cursor.rowcount
                conn.commit()
                return result
            finally:
                cursor.close()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg and attempts < retries:
                attempts += 1
                time.sleep(_WORKER_DB_RETRY_BASE_DELAY * attempts)
                continue
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

def insert_worker(worker_id: str, worker_type: str, title: str = "", description: str = "") -> bool:
    try:
        _worker_db_execute(
            "INSERT INTO workers (id, type, title, description, status) VALUES (?, ?, ?, ?, 'running')",
            (worker_id, worker_type, title, description),
        )
        return True
    except Exception as exc:
        logger.warning("Failed to insert worker %s: %s", worker_id, exc)
        return False

def update_worker(worker_id: str, **kwargs) -> bool:
    if not kwargs:
        return True
    
    # Filter valid columns
    valid_cols = {'type', 'title', 'description', 'status', 'progress', 'details', 'result', 'error_message'}
    cols = []
    vals = []
    for k, v in kwargs.items():
        if k in valid_cols:
            cols.append(f"{k} = ?")
            vals.append(v)
    
    if not cols:
        return True
        
    cols.append("updated_at = CURRENT_TIMESTAMP")
    query = f"UPDATE workers SET {', '.join(cols)} WHERE id = ?"
    vals.append(worker_id)
    
    try:
        _worker_db_execute(query, tuple(vals))
        return True
    except Exception as exc:
        logger.warning("Failed to update worker %s: %s", worker_id, exc)
        return False

def append_worker_stdout(worker_id: str, content: str, channel: str = 'stdout'):
    try:
        _worker_db_execute(
            "INSERT INTO worker_stdout (worker_id, channel, content) VALUES (?, ?, ?)",
            (worker_id, channel, content),
        )
    except Exception as exc:
        logger.warning("Failed to append worker stdout for %s: %s", worker_id, exc)

def get_worker_stdout(worker_id: str, channel: Optional[str] = None) -> str:
    query = "SELECT content FROM worker_stdout WHERE worker_id = ?"
    params = [worker_id]
    if channel:
        query += " AND channel = ?"
        params.append(channel)
    query += " ORDER BY timestamp ASC"
    
    rows = _worker_db_execute(query, tuple(params), fetch="all") or []
    return "\n".join(row['content'] for row in rows)

def get_active_workers() -> List[Dict[str, Any]]:
    rows = _worker_db_execute(
        "SELECT * FROM workers WHERE status = 'running' ORDER BY created_at DESC",
        fetch="all",
    ) or []
    return [dict(row) for row in rows]

def get_worker(worker_id: str) -> Optional[Dict[str, Any]]:
    row = _worker_db_execute(
        "SELECT * FROM workers WHERE id = ?",
        (worker_id,),
        fetch="one",
    )
    return dict(row) if row else None

def expire_running_workers(
    older_than_seconds: int = 300,
    status: str = 'error',
    reason: str = 'timeout',
    worker_id_prefix: Optional[str] = None,
) -> int:
    clauses = ["status = 'running'"]
    params: List[Any] = [status, reason]
    prefix = str(worker_id_prefix or "").strip()
    if prefix:
        clauses.append("id LIKE ?")
        params.append(prefix)
    try:
        seconds = max(0, int(older_than_seconds or 0))
    except Exception:
        seconds = 0
    if seconds > 0:
        clauses.append("updated_at < datetime('now', ?)")
        params.append(f"-{seconds} seconds")
    query = (
        "UPDATE workers SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP "
        f"WHERE {' AND '.join(clauses)}"
    )
    try:
        count = _worker_db_execute(query, tuple(params), timeout=0.5, retries=0)
        return int(count or 0)
    except Exception as exc:
        logger.warning("Failed to expire running workers: %s", exc)
        return 0
