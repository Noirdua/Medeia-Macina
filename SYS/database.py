from __future__ import annotations

import atexit
import sqlite3
import json
import ast
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

    def execute(self, query: str, params: tuple = ()): 
        attempts = 0
        while True:
            # Serialize access to the underlying sqlite connection to avoid
            # concurrent use from multiple threads which can trigger locks.
            with self._conn_lock:
                cursor = self.conn.cursor()
                try:
                    cursor.execute(query, params)
                    if not self.conn.in_transaction:
                        self.conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    # Retry a few times on transient lock errors
                    if 'locked' in msg and attempts < _DB_EXEC_RETRY_MAX:
                        attempts += 1
                        delay = _DB_EXEC_RETRY_BASE_DELAY * attempts
                        log(f"Database locked on execute; retry {attempts}/{_DB_EXEC_RETRY_MAX} in {delay:.2f}s")
                        try:
                            if not self.conn.in_transaction:
                                self.conn.rollback()
                        except Exception as exc:
                            logger.exception("Rollback failed while retrying locked execute: %s", exc)
                        time.sleep(delay)
                        continue
                    # Not recoverable or out of retries
                    if not self.conn.in_transaction:
                        try:
                            self.conn.rollback()
                        except Exception as exc:
                            logger.exception("Rollback failed in non-recoverable execute path: %s", exc)
                    raise
                except Exception as exc:
                    if not self.conn.in_transaction:
                        try:
                            self.conn.rollback()
                        except Exception as rb_exc:
                            logger.exception("Rollback failed during unexpected execute exception: %s", rb_exc)
                    logger.exception("Unexpected exception during DB execute: %s", exc)
                    raise

    def executemany(self, query: str, param_list: List[tuple]):
        attempts = 0
        while True:
            with self._conn_lock:
                cursor = self.conn.cursor()
                try:
                    cursor.executemany(query, param_list)
                    if not self.conn.in_transaction:
                        self.conn.commit()
                    return cursor
                except sqlite3.OperationalError as exc:
                    msg = str(exc).lower()
                    if 'locked' in msg and attempts < _DB_EXEC_RETRY_MAX:
                        attempts += 1
                        delay = _DB_EXEC_RETRY_BASE_DELAY * attempts
                        log(f"Database locked on executemany; retry {attempts}/{_DB_EXEC_RETRY_MAX} in {delay:.2f}s")
                        try:
                            if not self.conn.in_transaction:
                                self.conn.rollback()
                        except Exception as exc:
                            logger.exception("Rollback failed while retrying locked executemany: %s", exc)
                        time.sleep(delay)
                        continue
                    if not self.conn.in_transaction:
                        try:
                            self.conn.rollback()
                        except Exception as exc:
                            logger.exception("Rollback failed in non-recoverable executemany path: %s", exc)
                    raise
                except Exception as exc:
                    if not self.conn.in_transaction:
                        try:
                            self.conn.rollback()
                        except Exception as rb_exc:
                            logger.exception("Rollback failed during unexpected executemany exception: %s", rb_exc)
                    logger.exception("Unexpected exception during DB executemany: %s", exc)
                    raise

    @contextmanager
    def transaction(self):
        """Context manager for a database transaction.

        Transactions acquire the connection lock for the duration of the transaction
        to prevent other threads from performing concurrent operations on the
        same sqlite connection which can lead to locking issues.
        """
        if self.conn.in_transaction:
            # Already in a transaction, just yield
            yield self.conn
        else:
            # Hold the connection lock for the lifetime of the transaction
            self._conn_lock.acquire()
            try:
                self.conn.execute("BEGIN")
                try:
                    yield self.conn
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
            finally:
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
# Singleton instance
db = Database()

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


_ensure_log_db_schema()


def _log_worker_loop() -> None:
    """Background log writer using a temporary per-write connection with
    small retry/backoff and a file fallback when writes fail repeatedly.
    """
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
    """Save a configuration value to the database and invalidate config cache."""
    try:
        from SYS.config_crypto import encrypt_config_value
        val_str = encrypt_config_value({}, category, subtype, key, value)
    except Exception:
        val_str = json.dumps(value) if not isinstance(value, str) else value
    db.execute(
        "INSERT OR REPLACE INTO config (category, subtype, item_name, key, value) VALUES (?, ?, ?, ?, ?)",
        (category, subtype, item_name, key, val_str)
    )
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

        # Conservative JSON parsing: only attempt to decode when the value
        # looks like JSON (object/array/quoted string/true/false/null/number).
        # If JSON decoding fails, also attempt Python literal parsing (e.g., single-quoted
        # dict/list reprs) using ast.literal_eval as a safe fallback. If both
        # attempts fail, use the raw string value.
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
                            # Try parsing Python literal formats (single-quoted dicts/lists)
                            try:
                                parsed_val = ast.literal_eval(val)
                                debug(f"Parsed config value for key '{key}' using ast.literal_eval (non-JSON literal)")
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
            elif cat in ('provider', 'store'):
                continue
            elif cat == 'tool':
                # Retired namespace: load legacy tool rows into plugin.
                cat_dict = config.setdefault('plugin', {})
                sub_dict = cat_dict.setdefault(sub, {})
                if str(name or '').strip().lower() == 'default':
                    if key not in sub_dict:
                        sub_dict[key] = parsed_val
                else:
                    name_dict = sub_dict.setdefault(name, {})
                    if key not in name_dict:
                        name_dict[key] = parsed_val
            else:
                config.setdefault(cat, {})[key] = parsed_val

    return config


def get_config_all() -> Dict[str, Any]:
    """Retrieve all configuration from the database in the canonical plugin-centric dict format."""
    rows = db.fetchall("SELECT category, subtype, item_name, key, value FROM config")
    return rows_to_config(rows)


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

def expire_running_workers(older_than_seconds: int = 300, status: str = 'error', reason: str = 'timeout') -> int:
    # SQLITE doesn't have a simple way to do DATETIME - INTERVAL, so we'll use strftime/unixepoch if available
    # or just do regular update for all running ones for now as a simple fallback
    query = f"UPDATE workers SET status = ?, error_message = ? WHERE status = 'running'"
    try:
        _worker_db_execute(query, (status, reason), timeout=0.5, retries=0)
    except Exception:
        return 0
    return 0 # We don't easily get the rowcount from db.execute right now
