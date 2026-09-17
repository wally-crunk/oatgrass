"""SQLite-backed local Gazelle pacing coordination."""

from __future__ import annotations

import os
import platform
import sqlite3
import tempfile
import time
import uuid
import atexit
import getpass
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from oatgrass.__version__ import __version__

SPIGOT_API_ID = "local.oatgrass/spigot/v0"
DB_FILENAME = "spigot-v0.sqlite3"
APP_NAME = "Oatgrass"
APP_AUTHOR = "Oatgrass Contributors"
HEARTBEAT_INTERVAL_SECONDS = 600.0
STALE_PARTICIPANT_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 3
BOOTSTRAP_ACCOUNT_ID = "bootstrap"
TEST_PATH_ENV = "OATGRASS_SPIGOT_PATH"


class SpigotError(RuntimeError):
    """Raised when local Gazelle pacing state cannot be safely used."""


@dataclass(frozen=True)
class SpigotScope:
    tracker_name: str
    server_key: str
    account_identity: str = BOOTSTRAP_ACCOUNT_ID


@dataclass(frozen=True)
class Reservation:
    wait_seconds: float
    effective_slow: int
    penalty_wait_seconds: float
    active_participant_count: int
    reserved_for_monotonic: float | None = None


_session_uuid = str(uuid.uuid4())
_slow_declaration = 1
_path_override: Path | None = None


def _debug(message: str) -> None:
    try:
        from oatgrass import logger

        logger.get_logger().debug(f"[pacing via spigot] {message}")
    except Exception:
        return


def set_slow_declaration(value: int | None) -> None:
    global _slow_declaration
    if value is None:
        _slow_declaration = 1
        return
    _slow_declaration = max(1, int(value))


def set_path_for_tests(path: Path | None) -> None:
    global _path_override
    _path_override = path


def set_session_uuid_for_tests(session_uuid: str) -> None:
    global _session_uuid
    _session_uuid = session_uuid


def _temp_user_key() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return "".join(ch if ch.isalnum() else "_" for ch in getpass.getuser()) or "user"


def _shared_temp_root() -> Path:
    if platform.system().lower() == "windows":
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def state_dir() -> Path:
    if _path_override is not None:
        return _path_override.parent
    env_path = os.environ.get(TEST_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve().parent
    return _shared_temp_root() / f"oatgrass-spigot-{_temp_user_key()}"


def database_path() -> Path:
    if _path_override is not None:
        return _path_override
    env_path = os.environ.get(TEST_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return state_dir() / DB_FILENAME


def canonical_host(base_url: str) -> str:
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    host = (parsed.hostname or base_url).strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def same_domain(host_a: str, host_b: str) -> bool:
    return domain_key(host_a) == domain_key(host_b)


def domain_key(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2:] == ["ts", "net"]:
        return ".".join(parts[-3:])
    if len(parts) < 2:
        return host
    return ".".join(parts[-2:])


def account_identity(tracker_name: str, uid: int | str | None, username: str | None = None) -> str:
    if uid is None:
        return BOOTSTRAP_ACCOUNT_ID
    user_part = f":{username.strip().lower()}" if username else ""
    return f"{tracker_name.strip().lower()}:uid:{uid}{user_part}"


def make_scope(
    *,
    base_url: str,
    tracker_name: str,
    account_id: str | None = None,
) -> SpigotScope:
    server_key = canonical_host(base_url)
    if account_id and account_id != BOOTSTRAP_ACCOUNT_ID:
        server_key = domain_key(server_key)
    return SpigotScope(
        tracker_name=tracker_name.strip().lower(),
        server_key=server_key,
        account_identity=account_id or BOOTSTRAP_ACCOUNT_ID,
    )


class SpigotStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        _debug(f"using shared SPIGOT database {self.path}")

    def reserve_request(
        self,
        *,
        scope: SpigotScope,
        bucket_name: str,
        action: str | None,
        requests: int,
        seconds: float,
        min_interval_seconds: float,
        scheme_id: str | None = None,
        now: float | None = None,
    ) -> Reservation:
        now = time.monotonic() if now is None else float(now)
        self._ensure_parent()
        try:
            with self._connect() as conn:
                self._initialize(conn, now)
                self._heartbeat(conn, now)
                self._touch_scope(conn, scope, now)
                self._prune(conn, now)
                effective_slow = self._effective_slow(conn, now)
                participant_count = self._active_participant_count(conn, scope, now)
                own_reservation = self._own_reservation(
                    conn,
                    scope=scope,
                    bucket_name=bucket_name,
                    now=now,
                )
                if own_reservation is not None:
                    reserved_for, reservation_id = own_reservation
                    wait = self._compute_wait(
                        conn,
                        scope=scope,
                        bucket_name=bucket_name,
                        requests=max(1, int(requests)),
                        seconds=max(0.0, float(seconds)),
                        min_interval_seconds=max(0.0, float(min_interval_seconds)),
                        effective_slow=effective_slow,
                        now=reserved_for,
                        exclude_reservation_id=reservation_id,
                    )
                    if wait <= 0 and reserved_for <= now:
                        self._delete_reservation(conn, reservation_id)
                        self._record_request(
                            conn,
                            scope=scope,
                            bucket_name=bucket_name,
                            action=action,
                            requests=requests,
                            seconds=seconds,
                            now=now,
                        )
                        _debug(
                            "used shared SPIGOT reservation; "
                            f"scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity} "
                            f"scheme={scheme_id or '-'} bucket={bucket_name} action={action or '-'} "
                            f"participants={participant_count}"
                        )
                        return Reservation(0.0, effective_slow, 0.0, participant_count, None)
                    scheduled_for = max(reserved_for, reserved_for + wait)
                    if scheduled_for != reserved_for:
                        self._update_reservation(
                            conn,
                            reservation_id=reservation_id,
                            action=action,
                            requests=requests,
                            seconds=seconds,
                            min_interval_seconds=min_interval_seconds,
                            reserved_for=scheduled_for,
                            effective_slow=effective_slow,
                        )
                    wait_seconds = max(0.0, scheduled_for - now)
                    _debug(
                        "waiting on shared SPIGOT reservation; "
                        f"scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity} "
                        f"scheme={scheme_id or '-'} bucket={bucket_name} action={action or '-'} wait={wait_seconds:.3f}s "
                        f"reserved_for={scheduled_for:.3f} participants={participant_count}"
                    )
                    return Reservation(wait_seconds, effective_slow, 0.0, participant_count, scheduled_for)

                scheduled_for = self._earliest_request_time(
                    conn,
                    scope=scope,
                    bucket_name=bucket_name,
                    requests=max(1, int(requests)),
                    seconds=max(0.0, float(seconds)),
                    min_interval_seconds=max(0.0, float(min_interval_seconds)),
                    effective_slow=effective_slow,
                    now=now,
                )
                wait = scheduled_for - now
                if wait <= 0:
                    self._record_request(
                        conn,
                        scope=scope,
                        bucket_name=bucket_name,
                        action=action,
                        requests=requests,
                        seconds=seconds,
                        now=now,
                    )
                    _debug(
                        "recorded shared SPIGOT request without waiting; "
                        f"scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity} "
                        f"scheme={scheme_id or '-'} bucket={bucket_name} action={action or '-'} "
                        f"participants={participant_count}"
                    )
                    return Reservation(0.0, effective_slow, 0.0, participant_count, None)
                self._record_reservation(
                    conn,
                    scope=scope,
                    bucket_name=bucket_name,
                    action=action,
                    requests=requests,
                    seconds=seconds,
                    min_interval_seconds=min_interval_seconds,
                    reserved_for=scheduled_for,
                    effective_slow=effective_slow,
                )
                _debug(
                    "created shared SPIGOT reservation; "
                    f"scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity} "
                    f"scheme={scheme_id or '-'} bucket={bucket_name} action={action or '-'} wait={wait:.3f}s "
                    f"reserved_for={scheduled_for:.3f} participants={participant_count}"
                )
                return Reservation(wait, effective_slow, 0.0, participant_count, scheduled_for)
        except sqlite3.Error as exc:
            raise SpigotError(f"Cannot safely use SPIGOT database {self.path}: {exc}") from exc

    def record_throttle(
        self,
        *,
        scope: SpigotScope,
        retry_after: float | None,
        fallback_seconds: float,
        now: float | None = None,
    ) -> float:
        now = time.monotonic() if now is None else float(now)
        penalty = max(float(retry_after or 0.0), float(fallback_seconds), 0.0)
        self._ensure_parent()
        try:
            with self._connect() as conn:
                self._initialize(conn, now)
                self._heartbeat(conn, now)
                conn.execute(
                    """
                    INSERT INTO penalty_boxes(
                        tracker_name, server_key, account_identity, until_monotonic, created_monotonic
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(tracker_name, server_key, account_identity)
                    DO UPDATE SET
                        until_monotonic = max(until_monotonic, excluded.until_monotonic),
                        created_monotonic = excluded.created_monotonic
                    """,
                    (
                        scope.tracker_name,
                        scope.server_key,
                        scope.account_identity,
                        now + penalty,
                        now,
                    ),
                )
                _debug(
                    "recorded shared SPIGOT throttle; "
                    f"scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity} "
                    f"penalty={penalty:.3f}s"
                )
            return penalty
        except sqlite3.Error as exc:
            raise SpigotError(f"Cannot safely use SPIGOT database {self.path}: {exc}") from exc

    def close_session(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        if not self.path.exists():
            return
        self._ensure_parent()
        try:
            with self._connect() as conn:
                self._initialize(conn, now)
                conn.execute(
                    "UPDATE participants SET clean_exit_monotonic = ? WHERE session_uuid = ?",
                    (now, _session_uuid),
                )
                conn.execute("DELETE FROM participant_scopes WHERE session_uuid = ?", (_session_uuid,))
                conn.execute("DELETE FROM reservations WHERE session_uuid = ?", (_session_uuid,))
                _debug(f"closed shared SPIGOT session {_session_uuid} in {self.path}")
        except sqlite3.Error as exc:
            raise SpigotError(f"Cannot safely close SPIGOT session in {self.path}: {exc}") from exc

    def _ensure_parent(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
        except OSError as exc:
            raise SpigotError(f"Cannot create SPIGOT state directory {self.path.parent}: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        _debug(f"opening shared SPIGOT database {self.path}")
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level="IMMEDIATE")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self, conn: sqlite3.Connection, now: float) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                session_uuid TEXT PRIMARY KEY,
                app_version TEXT NOT NULL,
                slow_declaration INTEGER NOT NULL,
                heartbeat_monotonic REAL NOT NULL,
                clean_exit_monotonic REAL
            );
            CREATE TABLE IF NOT EXISTS participant_scopes (
                session_uuid TEXT NOT NULL,
                tracker_name TEXT NOT NULL,
                server_key TEXT NOT NULL,
                account_identity TEXT NOT NULL,
                last_seen_monotonic REAL NOT NULL,
                PRIMARY KEY(session_uuid, tracker_name, server_key, account_identity)
            );
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracker_name TEXT NOT NULL,
                server_key TEXT NOT NULL,
                account_identity TEXT NOT NULL,
                bucket_name TEXT NOT NULL,
                action TEXT,
                requests INTEGER NOT NULL,
                seconds REAL NOT NULL,
                started_monotonic REAL NOT NULL,
                expires_monotonic REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_request_events_scope
                ON request_events(tracker_name, server_key, account_identity, bucket_name, expires_monotonic);
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_uuid TEXT NOT NULL,
                tracker_name TEXT NOT NULL,
                server_key TEXT NOT NULL,
                account_identity TEXT NOT NULL,
                bucket_name TEXT NOT NULL,
                action TEXT,
                requests INTEGER NOT NULL,
                seconds REAL NOT NULL,
                min_interval_seconds REAL NOT NULL,
                effective_slow INTEGER NOT NULL,
                reserved_for_monotonic REAL NOT NULL,
                expires_monotonic REAL NOT NULL,
                created_monotonic REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_owner
                ON reservations(session_uuid, tracker_name, server_key, account_identity, bucket_name);
            CREATE INDEX IF NOT EXISTS idx_reservations_scope
                ON reservations(tracker_name, server_key, account_identity, bucket_name, expires_monotonic);
            CREATE TABLE IF NOT EXISTS penalty_boxes (
                tracker_name TEXT NOT NULL,
                server_key TEXT NOT NULL,
                account_identity TEXT NOT NULL,
                until_monotonic REAL NOT NULL,
                created_monotonic REAL NOT NULL,
                PRIMARY KEY(tracker_name, server_key, account_identity)
            );
            CREATE TABLE IF NOT EXISTS account_aliases (
                tracker_name TEXT NOT NULL,
                host TEXT NOT NULL,
                username TEXT NOT NULL,
                uid TEXT NOT NULL,
                canonical_host TEXT NOT NULL,
                updated_monotonic REAL NOT NULL,
                PRIMARY KEY(tracker_name, host, username, uid)
            );
            """
        )
        api_id = conn.execute("SELECT value FROM metadata WHERE key = 'api_id'").fetchone()
        initialized = api_id is not None
        if api_id is not None and api_id[0] != SPIGOT_API_ID:
            raise SpigotError(
                f"SPIGOT database {self.path} uses unsupported API id {api_id[0]!r}"
            )
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('api_id', ?)",
            (SPIGOT_API_ID,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('created_by', ?)",
            (f"OATGRASS/{__version__}",),
        )
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('created_monotonic', ?)",
            (str(now),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('created_at_utc', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        action = "opened existing" if initialized else "initialized"
        _debug(f"{action} shared SPIGOT database {self.path}")

    def _heartbeat(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            """
            INSERT INTO participants(session_uuid, app_version, slow_declaration, heartbeat_monotonic)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_uuid) DO UPDATE SET
                app_version = excluded.app_version,
                slow_declaration = excluded.slow_declaration,
                heartbeat_monotonic = excluded.heartbeat_monotonic,
                clean_exit_monotonic = NULL
            """,
            (_session_uuid, f"OATGRASS/{__version__}", _slow_declaration, now),
        )
        _debug(f"refreshed shared SPIGOT session {_session_uuid}; slow={_slow_declaration}")

    def _touch_scope(self, conn: sqlite3.Connection, scope: SpigotScope, now: float) -> None:
        conn.execute(
            """
            INSERT INTO participant_scopes(
                session_uuid, tracker_name, server_key, account_identity, last_seen_monotonic
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_uuid, tracker_name, server_key, account_identity)
            DO UPDATE SET last_seen_monotonic = excluded.last_seen_monotonic
            """,
            (_session_uuid, scope.tracker_name, scope.server_key, scope.account_identity, now),
        )
        _debug(
            "refreshed shared SPIGOT scope; "
            f"session={_session_uuid} scope={scope.tracker_name}/{scope.server_key}/{scope.account_identity}"
        )

    def _prune(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("DELETE FROM request_events WHERE expires_monotonic <= ?", (now,))
        conn.execute("DELETE FROM penalty_boxes WHERE until_monotonic <= ?", (now,))
        conn.execute("DELETE FROM reservations WHERE expires_monotonic <= ?", (now,))
        conn.execute(
            "DELETE FROM participant_scopes WHERE last_seen_monotonic <= ?",
            (now - STALE_PARTICIPANT_SECONDS,),
        )
        conn.execute(
            "DELETE FROM participants WHERE heartbeat_monotonic <= ?",
            (now - STALE_PARTICIPANT_SECONDS,),
        )

    def _effective_slow(self, conn: sqlite3.Connection, now: float) -> int:
        row = conn.execute(
            "SELECT max(slow_declaration) FROM participants WHERE heartbeat_monotonic > ?",
            (now - STALE_PARTICIPANT_SECONDS,),
        ).fetchone()
        return max(1, int(row[0] or 1))

    def _active_participant_count(
        self,
        conn: sqlite3.Connection,
        scope: SpigotScope,
        now: float,
    ) -> int:
        row = conn.execute(
            """
            SELECT count(DISTINCT ps.session_uuid)
            FROM participant_scopes ps
            JOIN participants p ON p.session_uuid = ps.session_uuid
            WHERE ps.tracker_name = ?
              AND ps.server_key = ?
              AND ps.account_identity = ?
              AND ps.last_seen_monotonic > ?
              AND p.heartbeat_monotonic > ?
              AND p.clean_exit_monotonic IS NULL
            """,
            (
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                now - STALE_PARTICIPANT_SECONDS,
                now - STALE_PARTICIPANT_SECONDS,
            ),
        ).fetchone()
        return max(1, int(row[0] or 1))

    def _compute_wait(
        self,
        conn: sqlite3.Connection,
        *,
        scope: SpigotScope,
        bucket_name: str,
        requests: int,
        seconds: float,
        min_interval_seconds: float,
        effective_slow: int,
        now: float,
        exclude_reservation_id: int | None = None,
    ) -> float:
        penalty_wait = self._penalty_wait(conn, scope, now)
        min_wait = self._min_interval_wait(
            conn, scope, now, min_interval_seconds, effective_slow, exclude_reservation_id
        )
        window_wait = self._window_wait(
            conn, scope, bucket_name, requests, seconds, effective_slow, now, exclude_reservation_id
        )
        return max(0.0, penalty_wait, min_wait, window_wait)

    def _earliest_request_time(
        self,
        conn: sqlite3.Connection,
        *,
        scope: SpigotScope,
        bucket_name: str,
        requests: int,
        seconds: float,
        min_interval_seconds: float,
        effective_slow: int,
        now: float,
        exclude_reservation_id: int | None = None,
    ) -> float:
        candidate = now
        while True:
            wait = self._compute_wait(
                conn,
                scope=scope,
                bucket_name=bucket_name,
                requests=requests,
                seconds=seconds,
                min_interval_seconds=min_interval_seconds,
                effective_slow=effective_slow,
                now=candidate,
                exclude_reservation_id=exclude_reservation_id,
            )
            if wait <= 0:
                return candidate
            candidate += wait

    def _penalty_wait(self, conn: sqlite3.Connection, scope: SpigotScope, now: float) -> float:
        row = conn.execute(
            """
            SELECT until_monotonic FROM penalty_boxes
            WHERE tracker_name = ? AND server_key = ? AND account_identity = ?
            """,
            (scope.tracker_name, scope.server_key, scope.account_identity),
        ).fetchone()
        return 0.0 if row is None else row[0] - now

    def _min_interval_wait(
        self,
        conn: sqlite3.Connection,
        scope: SpigotScope,
        now: float,
        min_interval_seconds: float,
        effective_slow: int,
        exclude_reservation_id: int | None = None,
    ) -> float:
        row = conn.execute(
            """
            SELECT max(started_at) FROM (
                SELECT started_monotonic AS started_at FROM request_events
                WHERE tracker_name = ? AND server_key = ? AND account_identity = ?
                UNION ALL
                SELECT reserved_for_monotonic AS started_at FROM reservations
                WHERE tracker_name = ? AND server_key = ? AND account_identity = ?
                  AND (? IS NULL OR id != ?)
                  AND expires_monotonic > ?
                  AND reserved_for_monotonic <= ?
            )
            """,
            (
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                exclude_reservation_id,
                exclude_reservation_id,
                now,
                now,
            ),
        ).fetchone()
        if row is None or row[0] is None:
            return 0.0
        return row[0] + (min_interval_seconds * effective_slow) - now

    def _window_wait(
        self,
        conn: sqlite3.Connection,
        scope: SpigotScope,
        bucket_name: str,
        requests: int,
        seconds: float,
        effective_slow: int,
        now: float,
        exclude_reservation_id: int | None = None,
    ) -> float:
        if seconds <= 0:
            return 0.0
        allowed = max(1, int(requests / effective_slow))
        active_window = self._active_window_seconds(
            conn, scope, bucket_name, seconds, now, exclude_reservation_id
        )
        row = conn.execute(
            """
            SELECT started_at FROM (
                SELECT started_monotonic AS started_at, expires_monotonic FROM request_events
                WHERE tracker_name = ?
                  AND server_key = ?
                  AND account_identity = ?
                  AND bucket_name = ?
                UNION ALL
                SELECT reserved_for_monotonic AS started_at, expires_monotonic FROM reservations
                WHERE tracker_name = ?
                  AND server_key = ?
                  AND account_identity = ?
                  AND bucket_name = ?
                  AND (? IS NULL OR id != ?)
            )
            WHERE expires_monotonic > ?
              AND started_at > ?
              AND started_at <= ?
            ORDER BY started_at ASC
            LIMIT 1 OFFSET ?
            """,
            (
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                exclude_reservation_id,
                exclude_reservation_id,
                now,
                now - active_window,
                now,
                allowed - 1,
            ),
        ).fetchone()
        if row is None:
            return 0.0
        return row[0] + active_window - now

    def _active_window_seconds(
        self,
        conn: sqlite3.Connection,
        scope: SpigotScope,
        bucket_name: str,
        requested_seconds: float,
        now: float,
        exclude_reservation_id: int | None = None,
    ) -> float:
        row = conn.execute(
            """
            SELECT max(seconds) FROM (
                SELECT seconds, expires_monotonic FROM request_events
                WHERE tracker_name = ?
                  AND server_key = ?
                  AND account_identity = ?
                  AND bucket_name = ?
                UNION ALL
                SELECT seconds, expires_monotonic FROM reservations
                WHERE tracker_name = ?
                  AND server_key = ?
                  AND account_identity = ?
                  AND bucket_name = ?
                  AND (? IS NULL OR id != ?)
            )
            WHERE expires_monotonic > ?
            """,
            (
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                exclude_reservation_id,
                exclude_reservation_id,
                now,
            ),
        ).fetchone()
        return max(float(requested_seconds), float(row[0] or 0.0))

    def _own_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        scope: SpigotScope,
        bucket_name: str,
        now: float,
    ) -> tuple[float, int] | None:
        row = conn.execute(
            """
            SELECT reserved_for_monotonic, id FROM reservations
            WHERE session_uuid = ?
              AND tracker_name = ?
              AND server_key = ?
              AND account_identity = ?
              AND bucket_name = ?
              AND expires_monotonic > ?
            """,
            (_session_uuid, scope.tracker_name, scope.server_key, scope.account_identity, bucket_name, now),
        ).fetchone()
        if row is None:
            return None
        return float(row[0]), int(row[1])

    def _reservation_expiry(
        self,
        *,
        reserved_for: float,
        seconds: float,
        min_interval_seconds: float,
        effective_slow: int,
    ) -> float:
        duration = max(float(seconds), float(min_interval_seconds) * max(1, int(effective_slow)), 1.0)
        return reserved_for + duration

    def _record_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        scope: SpigotScope,
        bucket_name: str,
        action: str | None,
        requests: int,
        seconds: float,
        min_interval_seconds: float,
        reserved_for: float,
        effective_slow: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO reservations(
                session_uuid, tracker_name, server_key, account_identity, bucket_name,
                action, requests, seconds, min_interval_seconds, effective_slow,
                reserved_for_monotonic, expires_monotonic, created_monotonic
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_uuid, tracker_name, server_key, account_identity, bucket_name)
            DO UPDATE SET
                action = excluded.action,
                requests = excluded.requests,
                seconds = excluded.seconds,
                min_interval_seconds = excluded.min_interval_seconds,
                effective_slow = excluded.effective_slow,
                reserved_for_monotonic = excluded.reserved_for_monotonic,
                expires_monotonic = excluded.expires_monotonic
            """,
            (
                _session_uuid,
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                action,
                int(requests),
                float(seconds),
                float(min_interval_seconds),
                int(effective_slow),
                float(reserved_for),
                self._reservation_expiry(
                    reserved_for=reserved_for,
                    seconds=seconds,
                    min_interval_seconds=min_interval_seconds,
                    effective_slow=effective_slow,
                ),
                time.monotonic(),
            ),
        )

    def _update_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        reservation_id: int,
        action: str | None,
        requests: int,
        seconds: float,
        min_interval_seconds: float,
        reserved_for: float,
        effective_slow: int,
    ) -> None:
        conn.execute(
            """
            UPDATE reservations
            SET action = ?,
                requests = ?,
                seconds = ?,
                min_interval_seconds = ?,
                effective_slow = ?,
                reserved_for_monotonic = ?,
                expires_monotonic = ?
            WHERE id = ?
            """,
            (
                action,
                int(requests),
                float(seconds),
                float(min_interval_seconds),
                int(effective_slow),
                float(reserved_for),
                self._reservation_expiry(
                    reserved_for=reserved_for,
                    seconds=seconds,
                    min_interval_seconds=min_interval_seconds,
                    effective_slow=effective_slow,
                ),
                int(reservation_id),
            ),
        )

    def _delete_reservation(self, conn: sqlite3.Connection, reservation_id: int) -> None:
        conn.execute("DELETE FROM reservations WHERE id = ?", (int(reservation_id),))

    def _record_request(
        self,
        conn: sqlite3.Connection,
        *,
        scope: SpigotScope,
        bucket_name: str,
        action: str | None,
        requests: int,
        seconds: float,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO request_events(
                tracker_name, server_key, account_identity, bucket_name, action,
                requests, seconds, started_monotonic, expires_monotonic
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope.tracker_name,
                scope.server_key,
                scope.account_identity,
                bucket_name,
                action,
                int(requests),
                float(seconds),
                now,
                now + float(seconds),
            ),
        )

    def record_account_identity(
        self,
        *,
        tracker_name: str,
        base_url: str,
        username: str,
        uid: int | str,
        now: float | None = None,
    ) -> str:
        now = time.monotonic() if now is None else float(now)
        tracker = tracker_name.strip().lower()
        host = canonical_host(base_url)
        uid_text = str(uid)
        username_key = username.strip().lower()
        try:
            with self._connect() as conn:
                self._initialize(conn, now)
                rows = conn.execute(
                    """
                    SELECT host, canonical_host FROM account_aliases
                    WHERE tracker_name = ? AND username = ? AND uid = ?
                    """,
                    (tracker, username_key, uid_text),
                ).fetchall()
                canonical = host
                for known_host, known_canonical in rows:
                    if same_domain(host, known_host):
                        canonical = min(host, known_canonical)
                        break
                conn.execute(
                    """
                    INSERT INTO account_aliases(
                        tracker_name, host, username, uid, canonical_host, updated_monotonic
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tracker_name, host, username, uid) DO UPDATE SET
                        canonical_host = excluded.canonical_host,
                        updated_monotonic = excluded.updated_monotonic
                    """,
                    (tracker, host, username_key, uid_text, canonical, now),
                )
                if canonical != host:
                    conn.execute(
                        """
                        UPDATE account_aliases SET canonical_host = ?
                        WHERE tracker_name = ? AND username = ? AND uid = ?
                        """,
                        (canonical, tracker, username_key, uid_text),
                    )
                return canonical
        except sqlite3.Error as exc:
            raise SpigotError(f"Cannot safely use SPIGOT database {self.path}: {exc}") from exc


def _close_session_at_exit() -> None:
    try:
        SpigotStore().close_session()
    except SpigotError:
        return


atexit.register(_close_session_at_exit)
