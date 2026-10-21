"""Dashboard Streamlit: pemantau email PO kebutuhan tenaga kerja."""
from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import streamlit as st
from openai import OpenAI

from po_monitor import gmail_client as gmail
from po_monitor.config import get_config
from po_monitor.listener import ACTIVE, ERROR, Listener
from po_monitor.llm import analyze_email
from po_monitor.pipeline import reprocess
from po_monitor.storage import Storage, format_ts

UI_REFRESH_SECONDS = 10

st.set_page_config(page_title="Monitor PO Tenaga Kerja", page_icon="📨", layout="wide")

cfg = get_config()

FILTER_LABELS = {
    "Semua": "all",
    "Hanya PO": "po",
    "Bukan PO": "non_po",
    "Perlu review": "review",
    "Error": "error",
}


@st.cache_resource
def get_store(db_path: str) -> Storage:
    return Storage(db_path)


@st.cache_resource
def get_openai(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)


store = get_store(str(cfg.db_path))


def analyzer(email):
    return analyze_email(email, get_openai(cfg.openai_api_key), cfg.model, cfg.max_body_chars)


@st.cache_resource
def get_listener() -> Listener:
    """Satu listener background per proses `streamlit run`, berapa pun tab yang terbuka."""
    listener = Listener(cfg, store, analyzer, lambda: gmail.load_credentials(cfg), gmail.GmailClient)
    listener.start()
    return listener


listener = get_listener()


def reset_session(message: str | None = None) -> None:
    gmail.logout(cfg)
    st.session_state.clear()
    if message:
        st.session_state["auth_message"] = message
    listener.wake()


# --- Autentikasi Gmail ---------------------------------------------------------


def handle_oauth_callback() -> None:
    params = st.query_params
    if "error" in params:
        st.session_state["auth_message"] = f"Login Google dibatalkan/gagal: {params['error']}"
        st.query_params.clear()
    elif "code" in params:
        try:
            gmail.exchange_code(cfg, params["code"], params.get("state"))
            listener.wake()
        except Exception as e:
            st.session_state["auth_message"] = f"Gagal menyelesaikan login Google: {e}"
        st.query_params.clear()
        st.rerun()


def render_login() -> None:
    st.title("📨 Monitor PO Tenaga Kerja")
    if msg := st.session_state.pop("auth_message", None):
        st.error(msg)
    st.write("Hubungkan akun Gmail untuk mulai memantau email PO kebutuhan tenaga kerja. Akses yang diminta hanya baca (read-only).")
    if "auth_url" not in st.session_state:
        st.session_state["auth_url"] = gmail.authorization_url(cfg)
    url = html.escape(st.session_state["auth_url"], quote=True)
    # _top: di Streamlit Cloud app berjalan di dalam iframe, dan Google menolak login di iframe.
    st.markdown(
        f'<a href="{url}" target="_top" style="display:inline-block;padding:0.5rem 1rem;'
        'border-radius:0.5rem;background:#1a73e8;color:white;text-decoration:none;font-weight:600;">'
        "Login dengan Google</a>",
        unsafe_allow_html=True,
    )


handle_oauth_callback()

if "account" not in st.session_state:
    creds = gmail.load_credentials(cfg)
    if creds is None:
        render_login()
        st.stop()
    try:
        st.session_state["account"] = gmail.GmailClient(creds).profile_email()
    except gmail.GmailAuthError as e:
        reset_session(str(e))
        st.rerun()
    except Exception as e:
        st.error(f"Gagal terhubung ke Gmail: {e}")
        st.stop()
elif not cfg.token_path.exists():  # logout dari tab lain
    reset_session()
    st.rerun()

with st.sidebar:
    st.markdown(f"**Akun Gmail**  \n{st.session_state['account']}")
    st.caption(
        f"Model: `{cfg.model}`  \n"
        f"Listener cek inbox tiap {cfg.poll_interval_seconds} detik selama aplikasi nyala. "
        "Email yang masuk saat aplikasi mati atau laptop sleep diabaikan."
    )
    if st.button("Logout", width="stretch"):
        reset_session()
        st.rerun()


# --- Status listener ---------------------------------------------------------------


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%Y-%m-%d %H:%M:%S")


@st.fragment(run_every=UI_REFRESH_SECONDS)
def live_status() -> None:
    s = listener.status()
    if s.state == ACTIVE:
        line = f"🟢 **Listener aktif** sejak {fmt_time(s.since)} · cek terakhir {fmt_time(s.last_check)}"
    elif s.state == ERROR:
        line = f"🟠 **Listener mengalami error** · cek terakhir {fmt_time(s.last_check)} (dicoba lagi otomatis)"
    else:
        line = "⏳ **Listener menunggu login Gmail...**"
    line += f" · {s.processed} email diproses ({s.po_found} PO)"
    if s.failed:
        line += f" · {s.failed} gagal"
    if s.queued:
        line += f" · {s.queued} antre"
    st.markdown(line)
    if s.resumed_at:
        st.caption(f"Laptop terdeteksi bangun dari sleep pukul {fmt_time(s.resumed_at)}; email selama sleep diabaikan.")
    if s.last_error:
        st.caption(f"⚠️ Error terakhir ({fmt_time(s.last_error_at)}): {s.last_error}")

    # Rerun penuh hanya jika data berubah, supaya metrik & tabel ikut diperbarui.
    if store.data_version() != st.session_state.get("data_version"):
        st.rerun(scope="app")


# --- Dashboard ---------------------------------------------------------------------


def status_label(row: dict, plain: bool = False) -> str:
    """Label status untuk tabel; `plain=True` tanpa emoji (untuk CSV)."""
    if row["status"] == "error":
        icon, text = "❌", "Error"
    elif row["status"] == "pending" or row["is_po"] is None:
        icon, text = "⏳", "Menunggu"
    elif row["is_po"]:
        icon, text = ("🟠", "PO (perlu review)") if row["needs_review"] else ("✅", "PO")
    else:
        icon, text = "", "Bukan PO"
    return text if plain or not icon else f"{icon} {text}"


def table_rows(rows: list[dict], plain: bool = False) -> list[dict]:
    """Baris tabel dashboard; dipakai juga untuk CSV supaya kolomnya selalu sama."""
    return [
        {
            "Tanggal": format_ts(r["received_at"]),
            "Pengirim": r["sender"],
            "Subject": r["subject"],
            "Status": status_label(r, plain),
            "Confidence": r["confidence"],
            "Perusahaan": r["client_company"] or "",
            "Total TK": r["total_headcount"],
            "Detail": r["summary"] or "",
        }
        for r in rows
    ]


def show(value) -> str:
    return "-" if value in (None, "") else str(value)


def render_metrics() -> None:
    m = store.metrics()
    cols = st.columns(5)
    cols[0].metric("Email diproses", m["total"])
    cols[1].metric("Email PO", m["po"])
    cols[2].metric("Perlu review", m["review"])
    cols[3].metric("Error", m["error"])
    cols[4].metric("Total tenaga kerja", m["headcount"])


def render_detail(message_id: str) -> None:
    d = store.get_email_detail(message_id)
    if d is None:
        return
    st.subheader(d["subject"] or "(tanpa subject)")
    st.caption(f"{d['sender']} · {format_ts(d['received_at'])}")

    left, right = st.columns([4, 1])
    with left:
        if d["status"] == "error":
            st.error(f"Gagal diproses: {d['error_message']}")
        elif d["status"] == "pending":
            st.info("Email belum diproses.")
        elif d["is_po"]:
            msg = f"**PO kebutuhan tenaga kerja** · confidence {d['confidence']:.2f}  \n{d['reason']}"
            (st.warning if d["needs_review"] else st.success)(
                ("🟠 Perlu review — " if d["needs_review"] else "") + msg
            )
        else:
            st.info(f"**Bukan PO** · confidence {d['confidence']:.2f}  \n{d['reason']}")
    with right:
        if st.button("🔁 Proses ulang", width="stretch"):
            with st.spinner("Memproses ulang dengan LLM..."):
                try:
                    reprocess(store, analyzer, cfg, message_id)
                except Exception:
                    pass  # status error & pesannya sudah tersimpan di DB
            st.rerun()

    tab_po, tab_email = st.tabs(["Data PO", "Email asli"])
    with tab_po:
        po = d["po"]
        if po is None:
            st.caption("Tidak ada data PO untuk email ini.")
        else:
            st.markdown(f"📝 {po.summary}")
            c1, c2, c3 = st.columns(3)
            c1.markdown(
                f"**Perusahaan pemesan:** {show(po.client_company)}  \n"
                f"**Nomor PO:** {show(po.po_number)}  \n"
                f"**Tanggal PO:** {show(po.po_date)}"
            )
            c2.markdown(
                f"**Kontak:** {show(po.contact_name)}  \n"
                f"**Email:** {show(po.contact_email)}  \n"
                f"**Telepon:** {show(po.contact_phone)}"
            )
            c3.markdown(
                f"**Site:** {show(po.site_name)}  \n"
                f"**Lokasi:** {show(po.work_location)}  \n"
                f"**Mulai:** {show(po.start_date)} · **Durasi:** {show(po.contract_duration)}"
            )
            st.markdown(f"**Benefit/fasilitas:** {show(po.benefits)}")
            if po.notes:
                st.markdown(f"**Catatan:** {po.notes}")
            positions = pd.DataFrame(
                [
                    {
                        "Posisi": show(p.title),
                        "Jumlah": p.quantity,
                        "Gender": show(p.gender),
                        "Usia": show(p.age_range),
                        "Kualifikasi": show(p.qualifications),
                        "Shift/jam kerja": show(p.shift_or_hours),
                        "Tarif/budget": show(p.rate_or_budget),
                    }
                    for p in po.positions
                ]
            )
            if positions.empty:
                st.caption("Posisi tidak terdeteksi.")
            else:
                st.dataframe(positions, hide_index=True, width="stretch")
    with tab_email:
        if d["attachments"]:
            st.caption("Lampiran (tidak dibaca): " + ", ".join(d["attachments"]))
        st.text(d["body_text"] or d["snippet"] or "(kosong)")


st.title("📨 Monitor PO Tenaga Kerja")
st.session_state["data_version"] = store.data_version()
live_status()
render_metrics()

f1, f2, f3 = st.columns([3, 2, 1], vertical_alignment="bottom")
filter_key = FILTER_LABELS[f1.radio("Filter", list(FILTER_LABELS), horizontal=True)]
search = f2.text_input("Cari pengirim / subject", placeholder="mis. PT Maju atau security")
rows = store.list_emails(filter_key, search)
f3.download_button(
    "⬇️ Download CSV",
    data=pd.DataFrame(table_rows(rows, plain=True)).to_csv(index=False).encode("utf-8-sig"),
    file_name="po_tenaga_kerja.csv",
    mime="text/csv",
    disabled=not rows,
    width="stretch",
)

if not rows:
    st.info("Belum ada email. Email yang masuk selama aplikasi nyala akan muncul di sini otomatis.")
    st.stop()

table = pd.DataFrame(table_rows(rows))
event = st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
        "Total TK": st.column_config.NumberColumn(format="%d"),
    },
    # Key ikut berubah saat data/filter berubah, sehingga pilihan baris (berdasarkan posisi)
    # tidak pindah ke email lain ketika ada email baru di atas tabel.
    key=f"email_table:{st.session_state['data_version']}:{filter_key}:{search}",
)

table_key = f"{st.session_state['data_version']}:{filter_key}:{search}"
if event.selection.rows:
    st.session_state["selected_id"] = rows[event.selection.rows[0]]["message_id"]
elif st.session_state.get("table_key") == table_key:
    st.session_state.pop("selected_id", None)  # user membatalkan pilihan
st.session_state["table_key"] = table_key

if selected_id := st.session_state.get("selected_id"):
    st.divider()
    render_detail(selected_id)
else:
    st.caption("Pilih baris pada tabel untuk melihat detail.")
