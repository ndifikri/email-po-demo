"""Listener email: hanya memproses email yang masuk selama aplikasi nyala.

Satu thread background per proses. Titik awal (historyId) disimpan di memori, jadi setiap
restart aplikasi atau laptop bangun dari sleep memulai pemantauan dari "sekarang".
"""
from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import AppConfig
from .gmail_client import GmailAuthError, HistoryExpired
from .pipeline import Analyzer, MailSource, ingest, reprocess
from .storage import Storage

THREAD_NAME = "po-monitor-listener"

WAITING_LOGIN = "waiting_login"
ACTIVE = "active"
ERROR = "error"


@dataclass
class ListenerStatus:
    state: str = WAITING_LOGIN
    since: float | None = None  # waktu titik awal saat ini
    last_check: float | None = None
    processed: int = 0
    po_found: int = 0
    failed: int = 0
    queued: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    resumed_at: float | None = None  # terakhir kali terdeteksi bangun dari sleep


class Listener:
    def __init__(
        self,
        cfg: AppConfig,
        store: Storage,
        analyze: Analyzer,
        load_credentials: Callable[[], object | None],
        make_client: Callable[[object], MailSource],
        clock: Callable[[], float] = time.time,
    ):
        self._cfg = cfg
        self._store = store
        self._analyze = analyze
        self._load_credentials = load_credentials
        self._make_client = make_client
        self._clock = clock
        self.sleep_gap = max(45, 3 * cfg.poll_interval_seconds)

        self._client: MailSource | None = None
        self._cursor: str | None = None
        self._queue: list[str] = []
        self._last_tick_end: float | None = None
        self._pending_done = False

        self._lock = threading.Lock()
        self._status = ListenerStatus()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Status (dibaca dashboard dari thread lain) ----------------------------------

    def status(self) -> ListenerStatus:
        with self._lock:
            return dataclasses.replace(self._status, queued=len(self._queue))

    def _update(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self._status, k, v)

    def _record_error(self, message: str, now: float) -> None:
        self._update(last_error=message, last_error_at=now)

    # --- Siklus ------------------------------------------------------------------------

    def tick(self) -> None:
        """Satu siklus listener. Tidak pernah raise."""
        now = self._clock()
        try:
            self._tick(now)
        except GmailAuthError as e:
            self._drop_client()
            self._update(state=WAITING_LOGIN)
            self._record_error(str(e), now)
        except Exception as e:  # noqa: BLE001 — listener tidak boleh mati
            self._update(state=ERROR)
            self._record_error(f"{type(e).__name__}: {e}", now)
        finally:
            self._last_tick_end = self._clock()
            self._update(last_check=self._last_tick_end)

    def _drop_client(self) -> None:
        self._client = None
        self._cursor = None
        self._queue.clear()

    def _rebaseline(self, now: float) -> None:
        self._cursor = self._client.current_history_id()
        self._queue.clear()
        self._update(since=now, state=ACTIVE)

    def _tick(self, now: float) -> None:
        if not self._cfg.token_path.exists():  # belum login atau sudah logout
            self._drop_client()
            self._update(state=WAITING_LOGIN, since=None)
            return

        if self._client is None:
            creds = self._load_credentials()
            if creds is None:
                self._update(state=WAITING_LOGIN, since=None)
                return
            self._client = self._make_client(creds)

        # Jeda diukur dari AKHIR siklus sebelumnya: siklus yang lama karena banyak email bukan sleep.
        slept = self._last_tick_end is not None and now - self._last_tick_end > self.sleep_gap
        if self._cursor is not None and slept:
            self._update(resumed_at=now)
            self._cursor = None

        if self._cursor is None:
            self._rebaseline(now)
            if not self._pending_done:
                self._pending_done = True
                self._process_pending(now)
            return

        try:
            ids, latest = self._client.list_added_since(self._cursor)
        except HistoryExpired:
            self._rebaseline(now)
            return
        self._cursor = latest
        self._update(state=ACTIVE)

        existing = self._store.existing_message_ids(ids)
        self._queue += [i for i in ids if i not in existing and i not in self._queue]
        self._drain(now)

    def _process_pending(self, now: float) -> None:
        for message_id in self._store.pending_message_ids():
            try:
                analysis = reprocess(self._store, self._analyze, self._cfg, message_id)
            except Exception as e:  # noqa: BLE001
                self._count_failure(message_id, e, now)
            else:
                self._count_success(analysis.is_po)

    def _drain(self, now: float) -> None:
        for _ in range(self._cfg.max_emails_per_cycle):
            if not self._queue:
                return
            message_id = self._queue[0]
            try:
                analysis = ingest(self._store, self._client, self._analyze, self._cfg, message_id)
            except GmailAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                if not self._store.existing_message_ids([message_id]):
                    # Gagal mengambil dari Gmail: biarkan di antrean, coba lagi siklus berikutnya.
                    raise
                self._queue.pop(0)
                self._count_failure(message_id, e, now)
                continue
            self._queue.pop(0)
            self._count_success(analysis.is_po)

    def _count_success(self, is_po: bool) -> None:
        with self._lock:
            self._status.processed += 1
            self._status.po_found += int(is_po)

    def _count_failure(self, message_id: str, error: Exception, now: float) -> None:
        with self._lock:
            self._status.failed += 1
        self._record_error(f"Email {message_id}: {error}", now)

    # --- Thread --------------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self._cfg.poll_interval_seconds)
            self._wake.clear()

    def wake(self) -> None:
        """Jalankan siklus berikutnya segera (mis. setelah login/logout)."""
        self._wake.set()

    def start(self) -> None:
        # Hot-reload Streamlit bisa membuat instance baru; hentikan listener lama dulu.
        for t in threading.enumerate():
            old = getattr(t, "listener", None)
            if t.name == THREAD_NAME and t.is_alive() and old is not self and old is not None:
                old.stop(timeout=5)
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.listener = self
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
