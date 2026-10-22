import dataclasses

import pytest

from po_monitor.config import load_config
from po_monitor.gmail_client import GmailAuthError, HistoryExpired
from po_monitor.listener import ACTIVE, ERROR, WAITING_LOGIN, Listener
from po_monitor.llm import LLMError
from po_monitor.pipeline import ingest, reprocess
from po_monitor.storage import Storage
from tests.helpers import make_email, non_po_analysis, po_analysis

T0 = 1_760_000_000.0


class FakeGmail:
    """Mailbox tiruan dengan historyId yang naik setiap ada email masuk."""

    def __init__(self, old_emails=()):
        self.history = 100
        self.events: list[tuple[int, str]] = []
        self.emails = {}
        self.fetched = []
        self.expired = False
        self.fail_fetch = set()
        self.auth_fail = False
        for e in old_emails:
            self.deliver(e)

    def deliver(self, email):
        self.history += 1
        self.events.append((self.history, email.message_id))
        self.emails[email.message_id] = email

    def current_history_id(self):
        return str(self.history)

    def list_added_since(self, history_id):
        if self.auth_fail:
            raise GmailAuthError("expired")
        if self.expired:
            self.expired = False
            raise HistoryExpired(history_id)
        ids = [mid for h, mid in self.events if h > int(history_id)]
        return ids, str(self.history)

    def fetch_message(self, message_id):
        if message_id in self.fail_fetch:
            raise ConnectionError("network down")
        self.fetched.append(message_id)
        return self.emails[message_id]


class FakeAnalyzer:
    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def __call__(self, email):
        self.calls.append(email.message_id)
        if email.message_id in self.fail_ids:
            raise LLMError("rate limit")
        return po_analysis() if "PO" in email.subject else non_po_analysis()


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def env(tmp_path):
    cfg = load_config(
        {
            "openai": {"api_key": "sk"},
            "google_oauth": {"client_id": "c", "client_secret": "s"},
            "auth": {"username": "u", "password": "p"},
        }
    )
    cfg = dataclasses.replace(cfg, token_path=tmp_path / "token.json")
    cfg.token_path.write_text("{}")  # sudah login
    store = Storage(tmp_path / "t.db")
    gmail = FakeGmail(old_emails=[make_email("old1", subject="PO lama"), make_email("old2")])
    analyzer = FakeAnalyzer()
    clock = Clock()
    listener = Listener(cfg, store, analyzer, lambda: object(), lambda creds: gmail, clock=clock)
    return dataclasses.make_dataclass("Env", ["cfg", "store", "gmail", "analyzer", "clock", "listener"])(
        cfg, store, gmail, analyzer, clock, listener
    )


def step(env, seconds=15):
    env.clock.advance(seconds)
    env.listener.tick()


def test_old_emails_ignored_new_processed_once(env):
    env.listener.tick()  # baseline
    assert env.listener.status().state == ACTIVE
    assert env.listener.status().since == T0
    step(env)
    assert env.analyzer.calls == []  # email lama tidak diproses

    env.gmail.deliver(make_email("n1", subject="PO Security"))
    step(env)
    step(env)
    assert env.analyzer.calls == ["n1"]
    s = env.listener.status()
    assert s.processed == 1 and s.po_found == 1
    assert env.store.get_email_detail("n1")["status"] == "processed"


def test_restart_ignores_emails_while_off(env):
    env.listener.tick()
    env.gmail.deliver(make_email("while_off"))
    # aplikasi "restart": listener baru dengan store yang sama
    fresh = Listener(env.cfg, env.store, env.analyzer, lambda: object(), lambda c: env.gmail, clock=env.clock)
    fresh.tick()
    env.clock.advance(15)
    fresh.tick()
    assert env.analyzer.calls == []


def test_sleep_gap_ignores_emails_during_sleep(env):
    env.listener.tick()
    env.gmail.deliver(make_email("during_sleep"))
    step(env, seconds=3600)  # bangun dari sleep
    assert env.listener.status().resumed_at == env.clock.t
    assert env.listener.status().since == env.clock.t
    env.gmail.deliver(make_email("after_wake"))
    step(env)
    assert env.analyzer.calls == ["after_wake"]


def test_long_cycle_is_not_sleep(env):
    env.listener.tick()
    slow_clock = env.clock

    def slow_analyze(email):
        slow_clock.advance(100)  # siklus memakan 100 detik
        return non_po_analysis()

    env.listener._analyze = slow_analyze
    env.gmail.deliver(make_email("a"))
    env.gmail.deliver(make_email("b"))
    step(env)  # siklus panjang: dua email × 100 detik
    env.gmail.deliver(make_email("c"))
    step(env)
    assert env.listener.status().resumed_at is None
    assert env.listener.status().processed == 3


def test_history_expired_rebaselines(env):
    env.listener.tick()
    env.gmail.expired = True
    step(env)
    assert env.listener.status().state == ACTIVE
    env.gmail.deliver(make_email("n1"))
    step(env)
    assert env.analyzer.calls == ["n1"]


def test_waiting_login_then_logout(env):
    env.cfg.token_path.unlink()
    env.listener.tick()
    assert env.listener.status().state == WAITING_LOGIN

    env.cfg.token_path.write_text("{}")  # login
    step(env)
    assert env.listener.status().state == ACTIVE
    env.gmail.deliver(make_email("n1"))
    step(env)
    assert env.analyzer.calls == ["n1"]

    env.cfg.token_path.unlink()  # logout
    env.gmail.deliver(make_email("n2"))
    step(env)
    assert env.listener.status().state == WAITING_LOGIN
    assert env.analyzer.calls == ["n1"]


def test_auth_error_waits_for_login(env):
    env.listener.tick()
    env.gmail.auth_fail = True
    step(env)
    s = env.listener.status()
    assert s.state == WAITING_LOGIN and "expired" in s.last_error


def test_max_per_cycle_keeps_rest_in_queue(env):
    env.cfg = dataclasses.replace(env.cfg, max_emails_per_cycle=2)
    env.listener._cfg = env.cfg
    env.listener.tick()
    for i in range(5):
        env.gmail.deliver(make_email(f"m{i}"))
    step(env)
    assert env.analyzer.calls == ["m0", "m1"]
    assert env.listener.status().queued == 3
    step(env)
    step(env)
    assert env.analyzer.calls == [f"m{i}" for i in range(5)]


def test_llm_error_does_not_block_others(env):
    env.analyzer.fail_ids = {"bad"}
    env.listener.tick()
    env.gmail.deliver(make_email("bad"))
    env.gmail.deliver(make_email("good"))
    step(env)
    assert env.store.get_email_detail("bad")["status"] == "error"
    assert env.store.get_email_detail("good")["status"] == "processed"
    s = env.listener.status()
    assert s.failed == 1 and s.processed == 1 and s.queued == 0


def test_fetch_error_retries_next_cycle(env):
    env.listener.tick()
    env.gmail.deliver(make_email("n1"))
    env.gmail.fail_fetch = {"n1"}
    step(env)
    s = env.listener.status()
    assert s.state == ERROR and "network down" in s.last_error and s.queued == 1
    env.gmail.fail_fetch = set()
    step(env)
    assert env.analyzer.calls == ["n1"]
    assert env.listener.status().state == ACTIVE


def test_pending_processed_on_start(env):
    env.store.save_email(make_email("leftover", subject="PO sisa"))
    env.listener.tick()
    assert env.analyzer.calls == ["leftover"]
    assert env.store.get_email_detail("leftover")["status"] == "processed"


def test_ingest_and_reprocess(env):
    env.gmail.deliver(make_email("x", subject="PO"))
    env.analyzer.fail_ids = {"x"}
    with pytest.raises(LLMError):
        ingest(env.store, env.gmail, env.analyzer, env.cfg, "x")
    assert env.store.get_email_detail("x")["status"] == "error"
    env.analyzer.fail_ids = set()
    assert reprocess(env.store, env.analyzer, env.cfg, "x").is_po
    assert env.store.get_email_detail("x")["status"] == "processed"


def test_thread_start_stop_replaces_old(env):
    import threading

    from po_monitor.listener import THREAD_NAME

    env.listener.start()
    second = Listener(env.cfg, env.store, env.analyzer, lambda: object(), lambda c: env.gmail)
    second.start()
    alive = [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]
    assert [t.listener for t in alive] == [second]
    second.stop(timeout=5)
