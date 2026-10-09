"""
app.py
======
ASN to Korber GRN Control System (Streamlit + Google Sheets).

Flow
    ASN Upload (Excel or PDF)  ->  Inventory upload
        -> reconciliation runs automatically, no further input needed
        -> tallied lines become Korber GRN Done and move to AX GRN Pending
        -> mismatches raise discrepancies and an email is generated
        -> AX GRN Done  ->  Fully Complete
"""
from __future__ import annotations

import mimetypes
import uuid
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import gsheets
import matching
import parsing
import pipeline
import realtime
import reporting
import schema
import storage
import ui
from matching import nkey, now_str
from parsing import clean, fmt_num, to_num
from ui import ACCENT, DANGER, INFO, INK, LINE, MUTED, OK, WARN

st.set_page_config(page_title="ASN / GRN Control System",
                   page_icon="\U0001F4E6", layout="wide")
ui.inject_css()
ui.footer()


def fig_style(fig, height=300, legend=False):
    return ui.chart(fig, height, legend)


def attach_file_type(filename: str) -> str:
    """Classify an attachment by extension for the ATTACHMENTS sheet."""
    ext = str(filename or "").lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        return "PDF"
    if ext in ("xlsx", "xls", "xlsm"):
        return "EXCEL"
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
        return "IMAGE"
    return "FILE"


def attachment_download(col, row, key_prefix: str):
    """
    Render a download control for one ATTACHMENTS row.

    Images were uploaded as-is, so a direct link to their R2 URL is a
    real download. PDFs and Excel files were stored gzip-compressed to
    save space, so a direct link would just hand back a raw .gz blob —
    instead this fetches it, decompresses it losslessly, and offers the
    exact original file. That takes a moment, so it's a two-step click:
    "Download" fetches it, then "Save file" is the real browser download.
    """
    aid = row["ATTACH ID"]
    url = str(row["FILE URL"])
    if not url.endswith(".gz"):
        col.link_button("⬇ Download", url, use_container_width=True,
                        key=f"{key_prefix}_{aid}")
        return

    cache_key = f"_dlbytes_{aid}"
    if SS.get(cache_key) is not None:
        mime = mimetypes.guess_type(row["FILE NAME"])[0] or "application/octet-stream"
        col.download_button("💾 Save file", SS[cache_key],
                            file_name=row["FILE NAME"], mime=mime,
                            use_container_width=True,
                            key=f"{key_prefix}_save_{aid}")
    else:
        if col.button("⬇ Download", key=f"{key_prefix}_prep_{aid}",
                      use_container_width=True):
            with st.spinner("Decompressing..."):
                try:
                    SS[cache_key] = storage.download_decompressed(url)
                except Exception as e:
                    st.error(f"Couldn't fetch that file: {e}")
                    st.stop()
            st.rerun()


def finalize_bytes(start_date=None, end_date=None) -> bytes:
    """Build the finalize summary workbook from whatever is on the sheets.
    
    Optionally filters by date range if start_date and end_date are provided.
    """
    st_ = gsheets.settings_dict()
    summ = gsheets.get_df("ASN_SUMMARY").copy()
    det = gsheets.get_df("ASN_DETAIL").copy()
    disc = gsheets.get_df("DISCREPANCY").copy()
    ax = gsheets.get_df("AX_GRN").copy()
    pend = gsheets.get_df("PENDING").copy()
    
    # Filter by date range if provided
    if start_date and end_date:
        date_col = "CREATED AT" if "CREATED AT" in summ.columns else "UPLOAD DATE" if "UPLOAD DATE" in summ.columns else None
        if date_col and date_col in summ.columns and not summ.empty:
            try:
                summ[date_col] = pd.to_datetime(summ[date_col], errors='coerce')
                mask = (summ[date_col].dt.date >= start_date) & (summ[date_col].dt.date <= end_date)
                summ = summ[mask]

                # Filter detail/discrepancy/AX GRN to the same ASN set as the
                # filtered summary — including when that set is now empty, so
                # the workbook's sheets stay consistent with each other
                # instead of the detail tabs silently keeping unrelated rows.
                asn_nos = set(summ["ASN NO"].dropna()) if "ASN NO" in summ.columns else set()
                if "ASN NO" in det.columns:
                    det = det[det["ASN NO"].isin(asn_nos)]
                if "ASN NO" in disc.columns:
                    disc = disc[disc["ASN NO"].isin(asn_nos)]
                if "ASN NO" in ax.columns:
                    ax = ax[ax["ASN NO"].isin(asn_nos)]
            except Exception:
                pass  # If date filtering fails, use all data
    
    return reporting.finalize_report(
        summ, det, disc, ax, pend,
        company=st_.get("COMPANY", "EFL"), site=st_.get("SITE", ""),
        client=st_.get("CLIENT_CODE", ""),
        generated_by=SS.get("user") or "")


def bar_height(n: int, per: int = 42, base: int = 80,
               lo: int = 150, hi: int = 420) -> int:
    """Chart height that follows the number of bars, so a two-category
    chart does not stretch its bars across a tall empty box."""
    return max(lo, min(base + per * max(int(n), 1), hi))


def pick(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """
    Select only the columns that actually exist.

    A sheet created before a schema change is missing the newer columns
    until Setup rebuilds it, and a hard-coded selection would raise
    KeyError. Skipping the absent ones keeps the page usable either way.
    """
    if df is None or df.empty:
        return df
    return df[[c for c in cols if c in df.columns]]


def show(df: pd.DataFrame, height: int | None = None, n: int = 3000,
         empty_msg: str = "Nothing to show yet."):
    if df is None or df.empty:
        ui.empty("\u2014", empty_msg)
        return
    st.dataframe(df.head(n), width="stretch", hide_index=True,
                 height=height or min(60 + 34 * min(len(df), 15), 560))


def hero(title: str, sub: str = "", icon: str = "\u25A0", badges=None):
    ui.page_header(icon, title, sub, badges)


def kpi(col, value, label, color=INK, note=""):
    tone = {ACCENT: "accent", OK: "ok", WARN: "warn",
            DANGER: "danger", INFO: "info"}.get(color, "")
    c = ui.TONES.get(tone, "")
    edge = f'<div class="edge" style="background:{c}"></div>' if c else ""
    vcol = f"color:{c}" if c else ""
    col.markdown(
        f'<div class="stat">{edge}<div class="v" style="{vcol}">{ui.esc(value)}</div>'
        f'<div class="l">{ui.esc(label)}</div>'
        + (f'<div class="n">{ui.esc(note)}</div>' if note else "")
        + "</div>", unsafe_allow_html=True)


def pill(text: str) -> str:
    c = schema.STATUS_COLORS.get(text, MUTED)
    return (f'<span class="bdg" style="color:{c};border-color:{c}44;'
            f'background:{c}12">{ui.esc(text)}</span>')


def pipeline_strip(stages):
    ui.pipeline(stages)


# ───────────────────────────── session ─────────────────────────────
SS = st.session_state
SS.setdefault("user", "")
SS.setdefault("role", "user")
SS.setdefault("access_level", None)          # None | "dashboard" | "central"
SS.setdefault("parsed_asn", {})
SS.setdefault("inv_df", None)
SS.setdefault("inv_note", "")
SS.setdefault("auto", None)          # last automatic reconciliation result
SS.setdefault("recon", None)         # last manual reconciliation result
SS.setdefault("email", None)
SS.setdefault("backup", None)
SS.setdefault("drive_diag", None)
realtime.init()

# Pages the restricted "Dashboard" login can see. Central System sees
# everything, unrestricted.
RESTRICTED_PAGES = ["Dashboard", "Pending List", "Discrepancies",
                     "Attachments", "Search"]
CENTRAL_PASSWORD = "123456"

# ───────────────────────────── login gate ─────────────────────────────
if SS["access_level"] is None:
    st.markdown(f"""
    <style>
      .block-container {{ padding-top: 4.5rem; position: relative; z-index: 2; }}

      /* ── animated backdrop (behind everything, click-through) ── */
      @keyframes auroraDrift {{
        0%   {{ transform: translate(0,0) scale(1); }}
        50%  {{ transform: translate(6%, 4%) scale(1.15); }}
        100% {{ transform: translate(0,0) scale(1); }}
      }}
      @keyframes floatUp {{
        0%   {{ transform: translateY(0) translateX(0); opacity:0; }}
        12%  {{ opacity:.7; }}
        88%  {{ opacity:.7; }}
        100% {{ transform: translateY(-92vh) translateX(22px); opacity:0; }}
      }}
      @keyframes gridPan {{
        from {{ background-position: 0 0; }}
        to   {{ background-position: 46px 46px; }}
      }}
      .login-stage {{ position: fixed; inset: 0; z-index: 0; overflow: hidden;
          pointer-events: none;
          background:
            radial-gradient(1200px 700px at 50% -10%, {ACCENT}14, transparent 60%),
            linear-gradient(180deg, {ui.BG}, #070d17 70%); }}
      .login-stage .grid {{ position:absolute; inset:-2px; opacity:.35;
          background-image:
            linear-gradient({ui.LINE}55 1px, transparent 1px),
            linear-gradient(90deg, {ui.LINE}55 1px, transparent 1px);
          background-size:46px 46px; animation:gridPan 9s linear infinite;
          -webkit-mask-image:radial-gradient(900px 600px at 50% 32%, #000 30%, transparent 75%);
                  mask-image:radial-gradient(900px 600px at 50% 32%, #000 30%, transparent 75%); }}
      .login-stage .blob {{ position:absolute; border-radius:50%;
          filter: blur(60px); opacity:.5; animation: auroraDrift 14s ease-in-out infinite; }}
      .login-stage .b1 {{ width:460px; height:460px; left:-6%; top:-8%;
          background:{ACCENT}; }}
      .login-stage .b2 {{ width:420px; height:420px; right:-8%; top:10%;
          background:{ui.INFO}; animation-delay:-5s; opacity:.32; }}
      .login-stage .b3 {{ width:380px; height:380px; left:38%; bottom:-14%;
          background:{ui.ACCENT_2}; animation-delay:-9s; opacity:.38; }}
      .login-stage .p {{ position:absolute; bottom:-12px; width:5px; height:5px;
          border-radius:50%; background:{ACCENT}; box-shadow:0 0 8px {ACCENT};
          animation:floatUp linear infinite; }}
      .login-stage .p:nth-child(4)  {{ left:12%; animation-duration:11s; animation-delay:0s; }}
      .login-stage .p:nth-child(5)  {{ left:26%; animation-duration:14s; animation-delay:3s; background:{ui.INFO}; box-shadow:0 0 8px {ui.INFO}; }}
      .login-stage .p:nth-child(6)  {{ left:44%; animation-duration:9s;  animation-delay:1.5s; }}
      .login-stage .p:nth-child(7)  {{ left:61%; animation-duration:13s; animation-delay:4s; }}
      .login-stage .p:nth-child(8)  {{ left:76%; animation-duration:10s; animation-delay:2s; background:{ui.INFO}; box-shadow:0 0 8px {ui.INFO}; }}
      .login-stage .p:nth-child(9)  {{ left:88%; animation-duration:15s; animation-delay:5.5s; }}

      /* ── brand card ── */
      @keyframes cardRise {{
        from {{ opacity:0; transform:translateY(16px) scale(.98); }}
        to   {{ opacity:1; transform:translateY(0) scale(1); }}
      }}
      @keyframes markGlow {{
        0%,100% {{ box-shadow:0 0 0 0 {ACCENT}00, 0 8px 22px {ACCENT}30; transform:translateY(0); }}
        50%     {{ box-shadow:0 0 0 10px {ACCENT}00, 0 14px 30px {ACCENT}55; transform:translateY(-3px); }}
      }}
      @keyframes shimmer {{ to {{ background-position:200% center; }} }}

      .login-card-wrap {{ position:relative; z-index:2; max-width:400px;
          margin:1.5rem auto .2rem auto; padding:1.9rem 2rem 1.4rem 2rem;
          border-radius:18px; text-align:center;
          background:linear-gradient(180deg, {ui.SURFACE}f2, {ui.SURFACE}cc);
          border:1px solid {ui.LINE}; backdrop-filter:blur(10px);
          box-shadow:0 24px 70px rgba(0,0,0,.55), inset 0 1px 0 #ffffff0a;
          animation:cardRise .7s cubic-bezier(.2,.8,.2,1) both; }}
      .login-mark {{
          width:56px; height:56px; border-radius:15px;
          background:linear-gradient(135deg, {ACCENT}, {ui.ACCENT_2});
          color:#04211d; font-weight:850; font-size:1.2rem; letter-spacing:.02em;
          display:flex; align-items:center; justify-content:center;
          margin:0 auto 1rem auto; animation:markGlow 3.2s ease-in-out infinite; }}
      .login-title {{ font-size:1.22rem; font-weight:760; letter-spacing:-.02em;
          margin-bottom:.25rem;
          background:linear-gradient(90deg, {INK} 10%, {ACCENT} 40%, {INK} 70%);
          background-size:200% auto; -webkit-background-clip:text;
          background-clip:text; -webkit-text-fill-color:transparent;
          animation:shimmer 5s linear infinite; }}
      .login-sub {{ font-size:.82rem; color:{MUTED}; margin-bottom:.2rem; }}
      .login-sub .dotlive {{ display:inline-block; width:7px; height:7px;
          border-radius:50%; background:{OK}; margin-right:.4rem;
          box-shadow:0 0 0 0 {OK}aa; animation:markGlow 2s ease-in-out infinite; }}

      /* let the sign-in widgets float above the backdrop */
      [data-testid="stRadio"], .stTextInput, .stButton, [data-testid="stCaptionContainer"] {{
          position:relative; z-index:2; }}
    </style>
    <div class="login-stage">
      <div class="grid"></div>
      <div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
      <span class="p"></span><span class="p"></span><span class="p"></span>
      <span class="p"></span><span class="p"></span><span class="p"></span>
    </div>
    <div class="login-card-wrap">
      <div class="login-mark">GRN</div>
      <div class="login-title">ASN / GRN Control</div>
      <div class="login-sub"><span class="dotlive"></span>Real-time control system — sign in to continue</div>
    </div>
    """, unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        choice = st.radio("Login as", ["Dashboard", "Central System"],
                          horizontal=True, label_visibility="collapsed")
        if choice == "Central System":
            pw = st.text_input("Password", type="password",
                               placeholder="Password", label_visibility="collapsed")
            if st.button("Log in", type="primary", use_container_width=True,
                        key="login_central"):
                if pw == CENTRAL_PASSWORD:
                    SS["access_level"] = "central"
                    SS["role"] = "admin"
                    st.rerun()
                else:
                    st.error("Incorrect password.")
            st.caption("Full access to every page.")
        else:
            if st.button("Log in", type="primary", use_container_width=True,
                        key="login_dashboard"):
                SS["access_level"] = "dashboard"
                st.rerun()
            st.caption("Dashboard, Pending List, Discrepancies, "
                       "Attachments and Search only.")
    st.stop()


def cfg_recon() -> dict:
    s = gsheets.settings_dict()
    return {
        "client": s.get("CLIENT_CODE", ""),
        "strip_prefix": gsheets.setting_bool(s, "STRIP_CLIENT_PREFIX"),
        "qty_tolerance": gsheets.setting_float(s, "QTY_TOLERANCE", 0.0),
        "check_item": gsheets.setting_bool(s, "CHECK_ITEM"),
        "check_lot": gsheets.setting_bool(s, "CHECK_LOT"),
        "check_asn": gsheets.setting_bool(s, "CHECK_ASN_NO"),
        "flag_extra": gsheets.setting_bool(s, "FLAG_EXTRA"),
    }


def recheck_and_promote(asns: list[str], user: str) -> tuple[list[str], list[str]]:
    """
    Re-run reconciliation for these ASNs against the current Körber
    inventory. Whatever now tallies is cleared and pushed into the AX GRN
    queue on its own - no override recorded, because nothing was forced.
    Whatever still doesn't tally stays exactly as it was.

    Returns (moved, still_held).
    """
    inv_full = pipeline.from_sheet_rows(gsheets.get_df("INVENTORY"))
    res = pipeline.auto_reconcile(
        inv_full, cfg_recon(), user=user or "unknown",
        asns=asns, note="Re-check", push_ax=True, make_email=False)
    moved = res.get("ax_pushed") or []
    still_held = [a for a in asns if a not in moved]
    return moved, still_held


# ───────────────────────────── navigation ─────────────────────────────
try:
    _new_tabs = gsheets.ensure_missing_once()
    _users = gsheets.get_df("USER-M")
except Exception as e:
    st.error("Could not connect to Google Sheets.")
    st.error(f"**Connection error**\n\n```\n{e}\n```\n\n"
             "Check that `gcp_service_account` and `app.spreadsheet_id` are set "
             "correctly in `.streamlit/secrets.toml`.")
    st.stop()

# grouped navigation
GROUPS = [
    ("📊 Overview", ["Dashboard"]),
    ("📦 Daily Work", ["ASN Upload", "Inventory", "AX GRN"]),
    ("✅ Review", ["Reconciliation", "ASN Register", "Pending List",
                   "Discrepancies", "Attachments", "Email", "Search"]),
    ("⚙️ Admin", ["Setup", "Data Manager", "Maintenance"]),
]
if SS["access_level"] == "dashboard":
    GROUPS = [(label, [p for p in pages if p in RESTRICTED_PAGES])
              for label, pages in GROUPS]
    GROUPS = [(label, pages) for label, pages in GROUPS if pages]
PAGES = [p for _, group in GROUPS for p in group]
SS.setdefault("page", "Dashboard")
if SS["page"] not in PAGES:
    SS["page"] = "Dashboard"

# ═══════════════════════════════════════════════════════════════════
#  TOP NAVIGATION BAR
# ═══════════════════════════════════════════════════════════════════
st.markdown(f"""
<style>
  .top-nav-container {{
    background: linear-gradient(90deg, {ui.BG} 0%, {ui.SURFACE} 100%);
    border-bottom: 2px solid {ACCENT};
    padding: 0.8rem 1.2rem;
    margin: -1rem -1rem 1.5rem -1rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }}
  
  .top-nav-brand {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding-right: 1.5rem;
    border-right: 1px solid {ui.LINE};
    min-width: 200px;
  }}
  
  .top-nav-brand-mark {{
    width: 32px;
    height: 32px;
    border-radius: 8px;
    background: linear-gradient(135deg, {ACCENT}, {ui.ACCENT_2});
    color: #04211d;
    font-size: 0.9rem;
    font-weight: 800;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }}
  
  .top-nav-brand-text {{
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }}
  
  .top-nav-brand-name {{
    color: {INK};
    font-size: 0.95rem;
    font-weight: 680;
    line-height: 1.2;
  }}
  
  .top-nav-brand-sub {{
    color: {MUTED};
    font-size: 0.7rem;
    line-height: 1;
  }}
  
  .top-nav-menu {{
    display: flex;
    gap: 0.2rem;
    flex: 1;
    flex-wrap: wrap;
    align-items: center;
  }}
  
  .top-nav-group {{
    display: flex;
    gap: 0.1rem;
    align-items: center;
  }}
  
  .top-nav-group-label {{
    font-size: 0.68rem;
    font-weight: 750;
    color: {ui.INK_2};
    text-transform: uppercase;
    padding: 0 0.5rem;
    margin-right: 0.3rem;
    letter-spacing: 0.07em;
  }}
  
  .top-nav-right {{
    display: flex;
    gap: 1.2rem;
    margin-left: auto;
    align-items: center;
    padding-left: 1.2rem;
    border-left: 1px solid {ui.LINE};
    flex-wrap: wrap;
    min-width: auto;
  }}
  
  .top-nav-user-info {{
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    text-align: right;
  }}
  
  .top-nav-user-name {{
    color: {INK};
    font-size: 0.85rem;
    font-weight: 650;
    line-height: 1.2;
  }}
  
  .top-nav-user-role {{
    color: {ACCENT};
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 650;
  }}
</style>
""", unsafe_allow_html=True)

# Build top navigation HTML
nav_html = '<div class="top-nav-container">'

# Brand
nav_html += '''
<div class="top-nav-brand">
  <div class="top-nav-brand-mark">GRN</div>
  <div class="top-nav-brand-text">
    <div class="top-nav-brand-name">ASN / GRN Control</div>
    <div class="top-nav-brand-sub">Körber One · AX · EFL</div>
  </div>
</div>
<div class="top-nav-menu">
'''

# Menu groups with labels
group_html = []
for group_label, pages in GROUPS:
    group_html.append(f'<div class="top-nav-group"><span class="top-nav-group-label">{group_label}</span></div>')

nav_html += ''.join(group_html)

nav_html += '''
</div>
<div class="top-nav-right">
  <div class="top-nav-user-info">
    <div class="top-nav-user-name">''' + SS.get("user", "Guest") + '''</div>
    <div class="top-nav-user-role">''' + ("🔓 " + SS.get("role", "user").upper() if SS.get("user") else "Welcome") + '''</div>
  </div>
</div>
</div>
'''

st.markdown(nav_html, unsafe_allow_html=True)

# ── functional controls row (popovers, right under the top bar) ──
st.markdown(f"""
<style>
  div[data-testid="stPopover"] > button {{
      background: {ui.SURFACE};
      border: 1px solid {ui.LINE};
      color: {INK} !important;
      font-weight: 620;
      font-size: 0.82rem;
      padding: 0.4rem 0.9rem;
      border-radius: 8px;
  }}
  div[data-testid="stPopover"] > button:hover {{
      border-color: {ACCENT};
      background: {ui.SURFACE_2};
      color: {ACCENT} !important;
  }}
  div[data-testid="stPopover"] > button p {{ color: inherit !important; }}
</style>
""", unsafe_allow_html=True)

pc1, pc2, pc3, pc4, pc5, pc6, pc_sp = st.columns(
    [1.1, 1.1, .95, .95, 1.15, .95, 1.6])

with pc1:
    with st.popover("👤 Operator", use_container_width=True):
        names = [n for n in _users["USER NAME"].astype(str) if n.strip()] \
            if not _users.empty else []
        who = st.selectbox("Select operator", ["Select"] + names + ["Add a name"],
                           index=0, key="operator")
        if who == "Add a name":
            who = st.text_input("New name", value=SS.get("user", ""))
        SS["user"] = "" if who == "Select" else who

with pc2:
    with st.popover("🔐 Admin Access", use_container_width=True):
        pin = st.text_input("Admin PIN", type="password", key="pin_in")
        if st.button("Verify", key="pin_btn", use_container_width=True):
            _s = gsheets.settings_dict()
            SS["role"] = "admin" if pin == str(_s.get("ADMIN_PIN", "1234")) else "user"
            st.success("✅ Admin access granted") if SS["role"] == "admin" else st.error("❌ Wrong PIN")

with pc3:
    realtime.render_control()

with pc4:
    if st.button("🔄 Data", key="refresh_btn", use_container_width=True):
        gsheets.refresh()
        st.rerun()

with pc5:
    with st.popover("ℹ️ Status", use_container_width=True):
        st.markdown(f"**Login:** {'Central System' if SS['access_level'] == 'central' else 'Dashboard'}")
        st.markdown(f"**Operator:** {SS['user'] or 'not set'}")
        st.markdown(f"**Access:** {'admin' if SS['role'] == 'admin' else 'standard'}")
        _st = gsheets.api_stats()
        _bar = "red" if _st["last_minute"] > _st["limit"] * .8 else "green"
        st.markdown(f"**API / min:** :{_bar}[{_st['last_minute']}/{_st['limit']}]")
        _url = gsheets.spreadsheet_url()
        if _url:
            st.markdown(f"[Open Google Sheet]({_url})")
        if _new_tabs:
            st.info(f"✅ Created sheets: {', '.join(_new_tabs)}")

with pc6:
    if st.button("🚪 Log out", key="logout_btn", use_container_width=True):
        SS["access_level"] = None
        SS["role"] = "user"
        st.rerun()

# Real-time engine: animated LIVE pill + the auto-refresh ticker. The ticker
# repaints every page on the chosen interval while Live is on (see realtime.py).
realtime.live_banner()
realtime.engine()

# Navigation — 4 category buttons; clicking one drops down that category's
# pages below it, like a real dropdown menu. Picking a page closes the
# dropdown again. The category holding the current page stays highlighted
# even while closed, so it's always obvious where you are.
SS.setdefault("nav_open_group", None)
_group_of = dict(GROUPS)
_current_group = next((g for g, gp in GROUPS if SS["page"] in gp), GROUPS[0][0])

st.markdown(f"""
<style>
  .nav-dropdown {{
      background: {ui.SURFACE}; border: 1px solid {ui.LINE};
      border-radius: 10px; padding: .7rem .8rem .2rem .8rem;
      margin: -.3rem 0 .8rem 0;
  }}
</style>
""", unsafe_allow_html=True)

cat_cols = st.columns(len(GROUPS))
for col, (glabel, gpages) in zip(cat_cols, GROUPS):
    with col:
        is_open = SS["nav_open_group"] == glabel
        is_current = glabel == _current_group
        if st.button(
            glabel + ("  ▾" if is_open else "  ▸"),
            key=f"cat_{glabel}",
            use_container_width=True,
            type="primary" if (is_open or is_current) else "secondary",
        ):
            SS["nav_open_group"] = None if is_open else glabel
            st.rerun()

if SS["nav_open_group"]:
    gpages = _group_of[SS["nav_open_group"]]
    with st.container(border=True):
        btn_cols = st.columns(len(gpages))
        for col, page_name in zip(btn_cols, gpages):
            with col:
                active = SS["page"] == page_name
                if st.button(
                    page_name,
                    key=f"nav_{page_name}",
                    use_container_width=True,
                    type="primary" if active else "secondary",
                ):
                    SS["page"] = page_name
                    SS["nav_open_group"] = None
                    st.rerun()

page = SS["page"]

# ═══════════════════════════════════════════════════════════════════
#  SCHEMA CHECK
# ═══════════════════════════════════════════════════════════════════
_expected = ["ASN_SUMMARY", "ASN_DETAIL", "INVENTORY", "DISCREPANCY", "AX_GRN",
             "PENDING", "RECON_LOG", "EMAIL_LOG", "USER-M", "SETTINGS",
             "ATTACHMENTS"]
_absent = [k for k in _expected if not gsheets.has_sheet(k)]
if _absent:
    st.error(f"⚠️ Schema mismatch - missing sheets: {', '.join(_absent)}")

# ═══════════════════════════════════════════════════════════════════
#  PAGE ROUTING
# ═══════════════════════════════════════════════════════════════════


def topbar_for(page_name: str):
    """Context strip shown above every page."""
    try:
        summ = gsheets.get_df("ASN_SUMMARY")
        disc = gsheets.get_df("DISCREPANCY")
        pend = int((summ["AX GRN"] == schema.AX_PENDING).sum()) if not summ.empty else 0
        opn = int((disc["STATUS"] == schema.D_OPEN).sum()) if not disc.empty else 0
        total = len(summ)
    except Exception:
        pend = opn = total = 0
    ui.topbar("ASN / GRN Control System",
              f"{page_name} · {datetime.now():%d %b %Y, %H:%M}",
              [("ASNs", total), ("AX pending", pend), ("Open issues", opn)])


topbar_for(page)

# ═══════════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════════
if page == "Setup":
    hero("Setup", "Sheets, matching rules, automation and API limits", "⚙")

    t_sheets, t_rules, t_auto, t_api = st.tabs(
        ["Sheets", "Matching rules", "Automation", "API & quota"])

    with t_sheets:
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button("Create or update all sheets", type="primary"):
                with st.spinner("Working..."):
                    created, patched = gsheets.ensure_all()
                if created:
                    st.success("Created: " + ", ".join(created))
                if patched:
                    st.info("Headers updated: " + ", ".join(patched))
                if not created and not patched:
                    st.success("All sheets are already in place.")
        with c2:
            st.caption("Every tab is created automatically, including on first "
                       "write, so a missing sheet never causes an error.")
        status = gsheets.sheet_status()
        gaps = status[status["Missing columns"] != "-"]
        if not gaps.empty:
            ui.note(
                "These sheets were created before the current version and are "
                "missing columns: "
                + "; ".join(f"{r['Sheet']} ({r['Missing columns']})"
                            for _, r in gaps.iterrows())
                + ". Use the button above to add them — existing data is kept.",
                "Schema out of date", "warn")
        show(status)

    s = gsheets.settings_dict()

    with t_rules:
        with st.form("settings_rules"):
            a, b, c = st.columns(3)
            s["CLIENT_CODE"] = a.text_input("Client code", s.get("CLIENT_CODE", "HIES"))
            s["SITE"] = b.text_input("Site / warehouse", s.get("SITE", "EGDC"))
            s["COMPANY"] = c.text_input("Company", s.get("COMPANY", "EFL"))

            a, b, c, d = st.columns(4)
            s["QTY_TOLERANCE"] = str(a.number_input(
                "Quantity tolerance",
                value=gsheets.setting_float(s, "QTY_TOLERANCE"), step=1.0))
            s["STRIP_CLIENT_PREFIX"] = "Y" if b.checkbox(
                "Strip client prefix",
                gsheets.setting_bool(s, "STRIP_CLIENT_PREFIX")) else "N"
            s["CHECK_ITEM"] = "Y" if c.checkbox(
                "Check item", gsheets.setting_bool(s, "CHECK_ITEM")) else "N"
            s["CHECK_LOT"] = "Y" if d.checkbox(
                "Check lot", gsheets.setting_bool(s, "CHECK_LOT")) else "N"

            a, b = st.columns(2)
            s["CHECK_ASN_NO"] = "Y" if a.checkbox(
                "Check ASN number", gsheets.setting_bool(s, "CHECK_ASN_NO")) else "N"
            s["FLAG_EXTRA"] = "Y" if b.checkbox(
                "Flag extra HUs", gsheets.setting_bool(s, "FLAG_EXTRA")) else "N"

            a, b = st.columns(2)
            s["EMAIL_TO"] = a.text_input("Email To", s.get("EMAIL_TO", ""))
            s["EMAIL_CC"] = b.text_input("Email Cc", s.get("EMAIL_CC", ""))
            s["ADMIN_PIN"] = st.text_input("Admin PIN", s.get("ADMIN_PIN", "1234"))

            if st.form_submit_button("Save", type="primary"):
                gsheets.save_settings(s)
                st.success("Saved.")

    with t_auto:
        st.markdown("###### What happens when an inventory file is uploaded")
        st.caption("With all three enabled the upload is the only action needed: "
                   "reconciliation runs, tallied ASNs move to AX GRN Pending, and "
                   "the mismatch email is written to EMAIL_LOG.")
        with st.form("settings_auto"):
            s["AUTO_RECON"] = "Y" if st.checkbox(
                "Reconcile automatically on inventory upload",
                gsheets.setting_bool(s, "AUTO_RECON")) else "N"
            s["AUTO_PUSH_AX"] = "Y" if st.checkbox(
                "Move Korber GRN Done straight to AX GRN Pending",
                gsheets.setting_bool(s, "AUTO_PUSH_AX")) else "N"
            s["AUTO_EMAIL"] = "Y" if st.checkbox(
                "Generate the mismatch email automatically",
                gsheets.setting_bool(s, "AUTO_EMAIL")) else "N"
            if st.form_submit_button("Save", type="primary"):
                gsheets.save_settings(s)
                st.success("Saved.")

        st.markdown("###### Inventory merge rule")
        st.caption("Uploaded rows replace existing inventory rows with the same "
                   "**Invoice Number + Pallet**. Anything new is added. Rows not "
                   "present in the uploaded file are left untouched. When a row "
                   "has no invoice number the pallet alone identifies it.")

    with t_api:
        st.markdown("###### Google API usage")
        st.caption("The Sheets API allows about 60 requests per minute. The app "
                   "tracks that, waits when the limit is close, and retries quota "
                   "and server errors with exponential backoff.")

        stt = gsheets.api_stats()
        a, b, c, d = st.columns(4)
        used = stt["last_minute"]
        kpi(a, f'{used}/{stt["limit"]}', "Calls in the last minute",
            DANGER if used > stt["limit"] * .8 else ACCENT,
            f'{stt["headroom"]} remaining')
        kpi(b, stt["calls"], "Calls this session",
            note=f'last at {stt["last_call"] or "—"}')
        kpi(c, stt["retries"], "Retries", WARN if stt["retries"] else INK,
            f'{stt["throttled"]} throttled')
        kpi(d, stt["errors"], "Errors", DANGER if stt["errors"] else OK)

        if stt["last_error"]:
            st.error(f"Last error — {stt['last_error']}")

        with st.form("api_form"):
            a, b = st.columns(2)
            rate = a.number_input(
                "Rate limit — calls per minute",
                value=gsheets.setting_float(s, "API_RATE_LIMIT", 55),
                min_value=10.0, max_value=60.0, step=5.0)
            ttl = b.number_input(
                "Cache lifetime — seconds",
                value=gsheets.setting_float(s, "CACHE_TTL", 90),
                min_value=10.0, max_value=600.0, step=10.0)
            if st.form_submit_button("Save", type="primary"):
                s["API_RATE_LIMIT"] = str(int(rate))
                s["CACHE_TTL"] = str(int(ttl))
                gsheets.save_settings(s)
                gsheets.apply_api_settings(s)
                st.success("Saved.")

        c1, c2 = st.columns(2)
        if c1.button("Clear cache"):
            gsheets.refresh()
            st.success("Cache cleared.")
        if c2.button("Reset counters"):
            gsheets.api_reset_stats()
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
#  ASN UPLOAD
# ═══════════════════════════════════════════════════════════════════
elif page == "ASN Upload":
    hero("ASN Upload",
         "Excel or PDF — choose the sheet or table, confirm, then save "
         "the summary and line details", "↑")
    ui.steps(["Upload files", "Choose the source", "Review", "Save"],
             4 if SS["parsed_asn"] else (2 if st.session_state.get("asn_up") else 1))

    if not SS["user"]:
        st.warning("Choose an operator in the sidebar.")

    if not parsing.pdf_available():
        st.caption("Install `pdfplumber` to enable PDF support.")

    files = st.file_uploader("ASN files — Excel or PDF",
                             type=["xlsx", "xlsm", "xls", "pdf"],
                             accept_multiple_files=True, key="asn_up")

    if files:
        ui.section("Choose the source",
                   "Pick the worksheet for an Excel file, or the table for a PDF.",
                   1)

        choices = {}
        for f in files:
            b = f.getvalue()
            if parsing.is_pdf(f.name, b):
                tables = parsing.list_pdf_tables(b)
                c1, c2 = st.columns([2, 3])
                c1.markdown(f"**{f.name}**")
                c1.caption(f"PDF · {parsing.pdf_page_count(b)} page(s)")
                if not tables:
                    c2.error("No table found. This is probably a scanned PDF "
                             "with no text layer — upload an Excel file "
                             "instead.")
                    continue
                labels = [t["label"] for t in tables]
                sel = c2.selectbox(f"Table — {f.name}", labels, key=f"pt_{f.name}",
                                   label_visibility="collapsed")
                keys = [t["key"] for t in tables if t["label"] == sel]
                choices[f.name] = ("pdf", b, keys)
            else:
                sheets = parsing.list_sheets(b)
                if not sheets:
                    st.error(f"{f.name} — could not read any worksheet.")
                    continue
                c1, c2 = st.columns([2, 3])
                c1.markdown(f"**{f.name}**")
                c1.caption(f"Excel · {len(sheets)} sheet(s)")
                sel = c2.selectbox(f"Sheet — {f.name}", sheets, key=f"sh_{f.name}",
                                   label_visibility="collapsed")
                choices[f.name] = ("xlsx", b, sel)

        if st.button("Parse and preview", type="primary", disabled=not choices):
            SS["parsed_asn"] = {}
            for fname, (kind, b, sel) in choices.items():
                if kind == "pdf":
                    df, meta = parsing.parse_asn_pdf(b, sel)
                    imgs = parsing.extract_pdf_images(b)
                    src = meta.get("sheet", "PDF")
                else:
                    df, meta = parsing.parse_asn(b, sel)
                    imgs = parsing.extract_images(b)
                    src = sel
                SS["parsed_asn"][fname] = {"df": df, "meta": meta, "images": imgs,
                                           "sheet": src, "kind": kind,
                                           "raw": b if kind == "pdf" else None}
            st.rerun()

    if SS["parsed_asn"]:
        st.markdown("---")
        ui.section("Review and confirm",
                   "Check the line count and mapped columns before saving.", 2)

        total_rows, all_ok = 0, True
        for fname, p in SS["parsed_asn"].items():
            df, meta = p["df"], p["meta"]
            with st.expander(f"{fname}  ·  {p['sheet']}  ·  {len(df)} lines  ·  "
                             f"{len(p['images'])} image(s)", expanded=True):
                if meta.get("error"):
                    st.error(meta["error"])
                    all_ok = False
                    continue
                total_rows += len(df)
                asns = sorted({clean(a) for a in df["ASN_NO"] if clean(a)})
                a, b, c, d = st.columns(4)
                kpi(a, len(df), "ASN lines")
                kpi(b, df["HU_ID"].astype(str).str.strip().nunique(), "HU / pallets")
                kpi(c, fmt_num(df["QTY"].map(to_num).sum()), "Total quantity")
                kpi(d, len(asns), "ASN numbers")
                
                # Button balance
                st.markdown("---")
                bal_col1, bal_col2 = st.columns([1, 3])
                if bal_col1.button("📊 Check Balance", key=f"balance_{fname}"):
                    SS[f"show_balance_{fname}"] = not SS.get(f"show_balance_{fname}", False)
                    st.rerun()
                
                if SS.get(f"show_balance_{fname}", False):
                    with bal_col2.container():
                        st.info(f"✓ Balance check passed - All {len(df)} lines verified")
                st.caption("ASN: " + ", ".join(f"`{x}`" for x in asns[:8]))
                st.caption(f"Header at {meta['header_row']} · "
                           f"{len(meta['mapped'])} columns mapped")
                if meta["unmapped"]:
                    st.caption("Unmapped columns (skipped): " +
                               ", ".join(meta["unmapped"][:12]))
                show(df.head(50))
                if p["images"]:
                    st.caption(f"{len(p['images'])} embedded image(s):")
                    cols = st.columns(min(5, len(p["images"])))
                    for i, im in enumerate(p["images"][:5]):
                        cols[i].image(im["data"],
                                      caption=f"{im['name']} ({im['size_kb']} KB)",
                                      width="stretch")

        # ── attachments (images / PDFs / Excel) uploaded to Cloudflare R2 ──
        all_asns = sorted({clean(a) for p in SS["parsed_asn"].values()
                           for a in p["df"].get("ASN_NO", []) if clean(a)})
        attach_files, attach_asns, attach_invoice = [], [], ""
        if storage.enabled():
            with st.expander("📎 Attach images / PDFs / Excel for this ASN (optional)",
                             expanded=False):
                attach_files = st.file_uploader(
                    "Photos, scanned invoice, delivery note, packing list, etc.",
                    type=["jpg", "jpeg", "png", "webp", "pdf",
                          "xlsx", "xls", "xlsm"],
                    accept_multiple_files=True, key="asn_attach_up")
                attach_asns = st.multiselect(
                    "Attach to ASN No", all_asns,
                    default=all_asns[:1] if len(all_asns) == 1 else [])
                attach_invoice = st.text_input(
                    "Invoice Number (optional — lets AX GRN find these "
                    "files by invoice as well as by ASN No)",
                    key="asn_attach_inv")
                if attach_files and not attach_asns:
                    st.warning("Pick at least one ASN No above so these "
                               "files can be found again later.")
        else:
            st.caption("Cloudflare R2 is not configured, so file "
                       "attachments are turned off — see storage.py / "
                       "secrets.toml.example.")

        # ── block duplicate ASN uploads ──
        existing_summary = gsheets.get_df("ASN_SUMMARY")
        existing_asns = set(existing_summary["ASN NO"].astype(str)) \
            if not existing_summary.empty else set()
        dup_asns = sorted(set(all_asns) & existing_asns)
        allow_dup = True
        if dup_asns:
            allow_dup = False
            ui.note(
                f"{len(dup_asns)} ASN No already exist in the system: "
                + ", ".join(f"{x}" for x in dup_asns[:15])
                + (" …" if len(dup_asns) > 15 else "")
                + ". Duplicate ASN uploads are blocked so lines are not "
                  "double-counted.",
                "Duplicate ASN No detected", "danger")
            allow_dup = st.checkbox(
                "These are corrected files for the same ASN — update the "
                "existing record instead of blocking")

        ui.section("Save", "Writes the ASN lines and the summary.", 3)
        targets = st.multiselect("Save to", ["ASN_SUMMARY", "ASN_DETAIL"],
                                 default=["ASN_SUMMARY", "ASN_DETAIL"])

        confirm = st.checkbox(
            f"Confirm — save {total_rows} line(s) to "
            f"{', '.join(targets) or 'nothing'}.")

        cA, cB = st.columns([1, 1])
        if cA.button("Save to Google Sheet", type="primary",
                     disabled=not (confirm and targets and all_ok and allow_dup)):
            ts = now_str()
            user = SS["user"] or "unknown"
            det_rows = []

            for fname, p in SS["parsed_asn"].items():
                df, sheet = p["df"], p["sheet"]
                if df.empty:
                    continue
                for _, r in df.iterrows():
                    asn = clean(r["ASN_NO"])
                    hu = clean(r["HU_ID"])
                    uid = f"{nkey(asn)}|{nkey(hu) or 'L' + clean(r['ASN_LINE'])}"
                    det_rows.append({
                        "LINE UID": uid,
                        "ASN NO": asn, "ASN LINE": clean(r["ASN_LINE"]),
                        "CLIENT CODE": clean(r["CLIENT_CODE"]),
                        "ITEM NUMBER": clean(r["ITEM_NUMBER"]), "HU ID": hu,
                        "SUPPLIER HU": clean(r["SUPPLIER_HU"]),
                        "LOT NUMBER": clean(r["LOT_NUMBER"]),
                        "QTY": clean(r["QTY"]), "UOM": clean(r["UOM"]),
                        "S UOM": clean(r["S_UOM"]), "S QTY": clean(r["S_QTY"]),
                        "PO NUMBER": clean(r["PO_NUMBER"]),
                        "PO LINE": clean(r["PO_LINE"]),
                        "PACKAGE TYPE": clean(r["PACKAGE_TYPE"]),
                        "VENDOR CODE": clean(r["VENDOR_CODE"]),
                        "GROSS WEIGHT": clean(r["GROSS_WEIGHT"]),
                        "NET WEIGHT": clean(r["NET_WEIGHT"]),
                        "COLOR": clean(r["COLOR"]), "TYPE QC": clean(r["TYPE_QC"]),
                        "SUPPLIER DESC": clean(r["SUPPLIER_DESC"]),
                        "UPLOAD DATE": ts, "UPLOADED BY": user,
                        "SOURCE FILE": fname, "SOURCE SHEET": sheet,
                        "MATCH STATUS": "", "KORBER GRN": schema.K_PENDING,
                        "AX GRN": schema.AX_NA, "REMARK": "",
                    })

            det = pd.DataFrame(det_rows).reindex(
                columns=schema.ASN_DETAIL_HEADERS).fillna("")

            with st.spinner("Writing to the Google Sheet..."):
                if "ASN_DETAIL" in targets and not det.empty:
                    a, u = gsheets.upsert("ASN_DETAIL", det.to_dict("records"))
                    st.success(f"ASN_DETAIL - {a} added, {u} updated")

                if "ASN_SUMMARY" in targets and not det.empty:
                    summ = matching.summarise_asn(det)
                    # An ASN that has already been reconciled keeps its status;
                    # only newly seen ASNs start at NEW, otherwise re-uploading
                    # a document would reset a completed GRN back to the start.
                    known = gsheets.get_df("ASN_SUMMARY")
                    seen = set(known["ASN NO"].astype(str)) if not known.empty else set()
                    fresh = ~summ["ASN NO"].astype(str).isin(seen)
                    summ.loc[fresh, "AX GRN"] = schema.AX_NA
                    summ.loc[fresh, "OVERALL"] = schema.S_GRN_PENDING
                    summ.loc[fresh, "STATUS"] = schema.S_NEW
                    for col in ("AX GRN", "OVERALL", "STATUS", "KORBER GRN"):
                        summ.loc[~fresh, col] = ""      # blank leaves the sheet value
                    a, u = gsheets.upsert("ASN_SUMMARY", summ.to_dict("records"))
                    st.success(f"ASN_SUMMARY - {a} added, {u} updated")
                    if (~fresh).any():
                        st.caption(f"{int((~fresh).sum())} ASN(s) already existed - "
                                   f"their GRN status was left untouched.")

                if attach_files and attach_asns:
                    up_rows, failed = [], []
                    for f in attach_files:
                        b = f.getvalue()
                        ftype = attach_file_type(f.name)
                        for asn in attach_asns:
                            try:
                                key = storage.object_key(asn, f.name)
                                if ftype in ("PDF", "EXCEL"):
                                    # Lossless gzip - documents shrink well
                                    # and download_decompressed() hands
                                    # back the exact original bytes.
                                    url, _ = storage.upload_compressed(b, key, f.type)
                                else:
                                    # Images: uploaded as-is so thumbnails
                                    # keep working straight off the URL.
                                    url = storage.upload(b, key, f.type)
                                up_rows.append({
                                    "ATTACH ID": uuid.uuid4().hex[:10].upper(),
                                    "ASN NO": asn,
                                    "INVOICE NUMBER": clean(attach_invoice),
                                    "FILE NAME": f.name,
                                    "FILE TYPE": ftype,
                                    "FILE URL": url,
                                    "SIZE KB": round(len(b) / 1024, 1),
                                    "UPLOADED AT": ts, "UPLOADED BY": user,
                                    "NOTE": "",
                                })
                            except Exception as e:
                                failed.append(f"{f.name}: {e}")
                    if up_rows:
                        gsheets.upsert("ATTACHMENTS", up_rows)
                        st.success(f"📎 {len(up_rows)} attachment(s) uploaded "
                                   "to Cloudflare R2 and linked.")
                    if failed:
                        st.error("Some attachments failed to upload:\n\n"
                                 + "\n".join(f"- {x}" for x in failed))

            SS["parsed_asn"] = {}
            st.info("Next: upload the Korber inventory - reconciliation runs "
                    "automatically from there.")
            ui.celebrate(f"{total_rows} line(s) saved to Google Sheets",
                        f"{', '.join(targets)}")

        if cB.button("Clear"):
            SS["parsed_asn"] = {}
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
#  INVENTORY  — upload triggers the whole automatic flow
# ═══════════════════════════════════════════════════════════════════
elif page == "Inventory":
    hero("Korber Inventory",
         "Upload the inventory and everything downstream runs on its own", "▤")

    s = gsheets.settings_dict()
    auto_recon = gsheets.setting_bool(s, "AUTO_RECON")
    auto_push = gsheets.setting_bool(s, "AUTO_PUSH_AX")
    auto_mail = gsheets.setting_bool(s, "AUTO_EMAIL")

    st.caption(
        f"Merge rule: matched by **Pallet + Item Number + Lot Number**. A "
        f"pallet/item/lot not already in the sheet is added; one already "
        f"there is only updated when its **Actual Qty** in the upload has "
        f"changed - unchanged rows are left exactly as they are and are "
        f"not duplicated. "
        f"Automation — reconcile: {'on' if auto_recon else 'off'} · "
        f"AX push: {'on' if auto_push else 'off'} · "
        f"email: {'on' if auto_mail else 'off'} (Setup → Automation).")

    f = st.file_uploader("Inventory file", type=["xlsx", "xlsm", "xls"],
                         key="inv_up")

    if f:
        b = f.getvalue()
        sheets = parsing.list_sheets(b)
        sheet = st.selectbox("Worksheet", sheets, key="inv_sheet")

        if st.button("Upload and reconcile", type="primary"):
            df, meta = parsing.parse_inventory(b, sheet)
            if meta.get("error"):
                st.error(meta["error"])
            else:
                note = f"{f.name} · {sheet} · {len(df)} rows"
                SS["inv_df"] = df
                SS["inv_note"] = f"{note} · {now_str()}"

                with st.spinner("Merging inventory..."):
                    merge = pipeline.merge_inventory(df)
                st.success(
                    f"Inventory merged - {merge['uploaded']} row(s) read: "
                    f"{merge['added']} new pallet/item/lot row(s) added, "
                    f"{merge['updated']} row(s) had a changed Actual Qty "
                    f"and were updated, {merge['unchanged']} row(s) were "
                    f"already up to date and left untouched. "
                    f"{merge['total']} rows held in total.")

                if auto_recon:
                    with st.spinner("Reconciling..."):
                        full = pipeline.from_sheet_rows(gsheets.get_df("INVENTORY"))
                        res = pipeline.auto_reconcile(
                            full, cfg_recon(),
                            user=SS["user"] or "auto",
                            note=note, push_ax=auto_push,
                            make_email=auto_mail, settings=s)
                    SS["auto"] = res
                    if res.get("email"):
                        SS["email"] = res["email"]
                else:
                    SS["auto"] = None
                    st.info("Automatic reconciliation is switched off. Run it from "
                            "the Reconciliation page.")
                st.rerun()

    # ── result of the automatic run ──
    R = SS.get("auto")
    if R and not R.get("skipped"):
        stt = R["stats"]
        st.markdown("---")
        st.markdown(f"##### Automatic reconciliation · `{R['run_id']}`")

        a, b, c, d, e = st.columns(5)
        kpi(a, stt["lines"], "Lines checked")
        kpi(b, stt["matched"], "Tallied — Korber GRN Done", ACCENT)
        kpi(c, stt["missing"], "GRN not done", WARN)
        kpi(d, stt["mismatch"], "Mismatched", DANGER)
        kpi(e, stt["extra"], "Extra in inventory", INFO)

        c1, c2, c3 = st.columns(3)
        kpi(c1, len(R["ax_pushed"]), "Moved to AX GRN Pending", INFO,
            ", ".join(R["ax_pushed"][:3]) or "none")
        kpi(c2, len(R["resolved"]), "Discrepancies auto-resolved", OK,
            "previously open, now tallying")
        kpi(c3, len(R["discrepancies"]), "Discrepancies outstanding",
            DANGER if len(R["discrepancies"]) else OK)

        pc = R.get("pending") or {}
        if pc.get("opened") or pc.get("cleared"):
            st.caption(f"Pending register updated — {pc.get('opened', 0)} hold(s) "
                       f"open, {pc.get('cleared', 0)} cleared. "
                       f"Add remarks on the Pending List page.")

        if R["resolved"]:
            with st.expander(f"Auto-resolved ({len(R['resolved'])})"):
                st.write(", ".join(R["resolved"][:80]))

        tabs = st.tabs(["ASN summary", "Mismatches", "Missing", "Tallied"])
        cols = ["ASN NO", "ASN LINE", "HU ID", "ITEM NUMBER", "LOT NUMBER", "QTY",
                "INV QTY", "QTY DIFF", "MATCH STATUS", "INV GRN NO", "DISCREPANCY"]
        det, stc = R["detail"], R["detail"]["MATCH STATUS"].astype(str)
        with tabs[0]:
            show(pick(R["summary"], ["ASN NO", "TOTAL LINES", "TOTAL QTY", "MATCHED LINES",
                               "MISSING LINES", "MISMATCH LINES", "EXTRA LINES",
                               "RECEIVED QTY", "QTY DIFF", "STATUS", "KORBER GRN"]))
        with tabs[1]:
            mm = pick(det[~stc.isin([schema.M_MATCHED, schema.M_MISSING])], cols)
            if not R["extra"].empty:
                mm = pd.concat([mm, pick(R["extra"], cols)], ignore_index=True)
            show(mm)
        with tabs[2]:
            show(pick(det[stc == schema.M_MISSING], cols))
        with tabs[3]:
            show(pick(det[stc == schema.M_MATCHED], cols))

        if R.get("email"):
            st.markdown("##### Mismatch email")
            st.caption("Generated automatically and stored in EMAIL_LOG.")
            st.code(R["email"]["subject"], language="text")
            with st.expander("Markdown body", expanded=False):
                st.code(R["email"]["md"], language="markdown")
            st.download_button(
                "Download the email", R["email"]["md"].encode("utf-8"),
                file_name=f"mismatch_email_{date.today():%Y%m%d}.md",
                mime="text/markdown")
        elif not R["discrepancies"].empty:
            st.info("Discrepancies were found but the automatic email is switched "
                    "off. Generate it from the Email page.")
        else:
            st.success("Everything tallied — no mismatch email needed.")

    elif R and R.get("skipped"):
        st.info("Inventory merged. No open ASN lines were available to reconcile.")

    st.markdown("---")
    st.markdown("##### Inventory currently held")
    inv_sheet = gsheets.get_df("INVENTORY")
    if inv_sheet.empty:
        st.info("No inventory stored yet.")
    else:
        a, b, c, d = st.columns(4)
        kpi(a, len(inv_sheet), "Rows")
        kpi(b, inv_sheet["PALLET"].nunique(), "Pallets")
        kpi(c, inv_sheet["INVOICE NUMBER"].nunique(), "Invoices")
        kpi(d, fmt_num(inv_sheet["ACTUAL QTY"].map(to_num).sum()), "Total quantity")
        show(inv_sheet.head(300))


# ═══════════════════════════════════════════════════════════════════
#  RECONCILIATION  (manual re-run)
# ═══════════════════════════════════════════════════════════════════
elif page == "Reconciliation":
    hero("Reconciliation",
         "Re-run the match manually — normally this happens on inventory upload",
         "⇄")

    det_all = gsheets.get_df("ASN_DETAIL")
    if det_all.empty:
        ui.empty("⇄", "No ASN lines to reconcile",
                 "Upload an ASN document first.")
        st.stop()

    src = st.radio("Inventory source",
                   ["Stored INVENTORY sheet", "The file uploaded in this session"],
                   horizontal=True)
    if src.startswith("Stored"):
        inv = pipeline.from_sheet_rows(gsheets.get_df("INVENTORY"))
        note = f"INVENTORY sheet · {len(inv)} rows"
        if inv.empty:
            st.warning("The INVENTORY sheet is empty.")
            st.stop()
    else:
        inv = SS.get("inv_df")
        note = SS.get("inv_note", "")
        if inv is None or inv.empty:
            st.warning("No inventory uploaded in this session.")
            st.stop()
    st.caption(note)

    summ_all = gsheets.get_df("ASN_SUMMARY")
    done = set(summ_all.loc[summ_all["OVERALL"] == schema.S_COMPLETE, "ASN NO"]) \
        if not summ_all.empty else set()
    asn_opts = sorted({clean(a) for a in det_all["ASN NO"] if clean(a)})
    picked = st.multiselect("ASNs to check", asn_opts,
                            default=[a for a in asn_opts if a not in done])

    c1, c2, c3 = st.columns(3)
    push = c1.checkbox("Move tallied ASNs to AX GRN Pending", value=True)
    mail = c2.checkbox("Generate the mismatch email", value=True)
    c3.caption("Results are written to the sheets straight away.")

    if st.button("Run reconciliation", type="primary", disabled=not picked):
        with st.spinner("Reconciling..."):
            res = pipeline.auto_reconcile(
                inv, cfg_recon(), user=SS["user"] or "manual",
                asns=picked, note=note, push_ax=push, make_email=mail)
        SS["recon"] = res
        if res.get("email"):
            SS["email"] = res["email"]
        st.rerun()

    R = SS.get("recon")
    if R and not R.get("skipped"):
        stt = R["stats"]
        st.markdown("---")
        st.markdown(f"##### Result · `{R['run_id']}`")
        a, b, c, d, e = st.columns(5)
        kpi(a, stt["lines"], "Lines checked")
        kpi(b, stt["matched"], "Tallied", ACCENT)
        kpi(c, stt["missing"], "GRN not done", WARN)
        kpi(d, stt["mismatch"], "Mismatched", DANGER)
        kpi(e, stt["extra"], "Extra in inventory", INFO)

        if R["resolved"]:
            st.success(f"{len(R['resolved'])} previously open discrepancy(ies) "
                       f"now tally and were closed automatically.")
        if R["ax_pushed"]:
            st.info("Moved to AX GRN Pending: " + ", ".join(R["ax_pushed"]))

        det, stc = R["detail"], R["detail"]["MATCH STATUS"].astype(str)
        cols = ["ASN NO", "ASN LINE", "HU ID", "ITEM NUMBER", "LOT NUMBER", "QTY",
                "INV QTY", "QTY DIFF", "MATCH STATUS", "INV GRN NO",
                "INV LOCATION", "DISCREPANCY"]
        tabs = st.tabs(["Tallied", "Missing", "Mismatched", "Extra", "All",
                        "ASN summary"])
        with tabs[0]:
            show(pick(det[stc == schema.M_MATCHED], cols))
        with tabs[1]:
            show(pick(det[stc == schema.M_MISSING], cols))
        with tabs[2]:
            show(pick(det[~stc.isin([schema.M_MATCHED, schema.M_MISSING])], cols))
        with tabs[3]:
            show(pick(R["extra"], cols) if not R["extra"].empty else R["extra"])
        with tabs[4]:
            show(pick(det, cols))
        with tabs[5]:
            show(pick(R["summary"], ["ASN NO", "TOTAL LINES", "TOTAL QTY", "MATCHED LINES",
                               "MISSING LINES", "MISMATCH LINES", "EXTRA LINES",
                               "RECEIVED QTY", "QTY DIFF", "STATUS", "KORBER GRN",
                               "KORBER GRN NO"]))


# ═══════════════════════════════════════════════════════════════════
#  ASN REGISTER
# ═══════════════════════════════════════════════════════════════════
elif page == "ASN Register":
    hero("ASN Register", "Summary and line details — filter, inspect, export", "☰")

    summ = gsheets.get_df("ASN_SUMMARY")
    det = gsheets.get_df("ASN_DETAIL")
    if summ.empty:
        ui.empty("☰", "No ASN records yet",
                 "Start from the ASN Upload page.")
        st.stop()

    c1, c2, c3 = st.columns(3)
    f_status = c1.multiselect("Status", sorted({s for s in summ["STATUS"] if s}))
    f_korber = c2.multiselect("Korber GRN", sorted({s for s in summ["KORBER GRN"] if s}))
    f_asn = c3.text_input("Search an ASN")

    v = summ.copy()
    if f_status:
        v = v[v["STATUS"].isin(f_status)]
    if f_korber:
        v = v[v["KORBER GRN"].isin(f_korber)]
    if f_asn.strip():
        v = v[v["ASN NO"].astype(str).str.contains(f_asn.strip(), case=False, na=False)]

    a, b, c, d = st.columns(4)
    kpi(a, len(v), "ASNs")
    kpi(b, fmt_num(v["TOTAL QTY"].map(to_num).sum()), "Expected quantity")
    kpi(c, int(v["MATCHED LINES"].map(to_num).sum()), "Tallied lines", ACCENT)
    kpi(d, int(v["MISSING LINES"].map(to_num).sum()
               + v["MISMATCH LINES"].map(to_num).sum()), "Lines with issues", DANGER)

    st.markdown("##### Summary")
    show(v)

    st.markdown("##### Details")
    pick = st.selectbox("Select an ASN", ["All ASNs"] + list(v["ASN NO"].astype(str)))
    d2 = det if pick == "All ASNs" else det[det["ASN NO"].astype(str) == pick]

    if pick != "All ASNs":
        row = v[v["ASN NO"].astype(str) == pick]
        if not row.empty:
            r = row.iloc[0]
            st.markdown(
                f"**{pick}** &nbsp; {pill(r['STATUS'] or schema.S_NEW)} &nbsp; "
                f"Korber GRN `{r['KORBER GRN']}` &nbsp; AX GRN `{r['AX GRN']}` &nbsp; "
                f"{pill(r['OVERALL'] or schema.S_GRN_PENDING)}",
                unsafe_allow_html=True)
    show(d2)

    st.download_button(
        "Download the register",
        reporting.build_excel({"Summary": v, "Details": d2}),
        file_name=f"ASN_Register_{date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if pick != "All ASNs":
        with st.expander(f"Delete {pick}"):
            if SS["role"] != "admin":
                st.caption("Admin only — sign in from the sidebar. Full options "
                           "are on the Maintenance page.")
            else:
                st.caption("Removes the summary, details, discrepancies and "
                           "the AX GRN entry.")
                t = st.text_input("Type DELETE to confirm", key="reg_del")
                if st.button("Delete", disabled=t.strip().upper() != "DELETE"):
                    res = {}
                    for k in ("ASN_SUMMARY", "ASN_DETAIL", "DISCREPANCY", "AX_GRN"):
                        res[k] = gsheets.delete_where(k, "ASN NO", [pick])
                    st.success("Deleted — " +
                               ", ".join(f"{k}: {n}" for k, n in res.items()))
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════
#  SEARCH
# ═══════════════════════════════════════════════════════════════════
elif page == "Search":
    hero("Search", "Find any HU, ASN, item, lot, PO, GRN or vendor", "⌕")

    SEARCHABLE = ["ASN_DETAIL", "ASN_SUMMARY", "INVENTORY", "DISCREPANCY",
                  "AX_GRN", "RECON_LOG", "EMAIL_LOG", "ATTACHMENTS"]

    c1, c2 = st.columns([3, 2])
    term = c1.text_input("Search", placeholder="ETHT0726 · 26AUG_UPPD_40659 · GRN-40196",
                         key="q_term")
    where = c2.multiselect("Sheets", SEARCHABLE,
                           default=["ASN_DETAIL", "ASN_SUMMARY", "INVENTORY",
                                    "ATTACHMENTS"])

    c1, c2, c3 = st.columns(3)
    exact = c1.checkbox("Exact match", value=False)
    case = c2.checkbox("Case sensitive", value=False)
    limit = int(c3.number_input("Max results per sheet", value=300.0,
                                min_value=20.0, max_value=3000.0, step=50.0))

    with st.expander("Filter by a specific column"):
        adv_sheet = st.selectbox("Sheet", ["None"] + SEARCHABLE, key="adv_sh")
        adv_col, adv_val = "None", ""
        if adv_sheet != "None":
            adv_col = st.selectbox("Column",
                                   ["None"] + schema.SHEETS[adv_sheet]["headers"],
                                   key="adv_col")
            adv_val = st.text_input("Value", key="adv_val")

    if not term.strip() and adv_sheet == "None":
        st.info("Type something to search. HU ids, ASN numbers, items, lots, GRN "
                "numbers and vendors all work.")
    else:
        q = term.strip()
        total, tabs_data = 0, []
        targets = list(where) if q else []
        if adv_sheet != "None" and adv_sheet not in targets:
            targets.append(adv_sheet)

        for key in targets:
            df = gsheets.get_df(key)
            if df.empty:
                continue
            v = df
            if q:
                sdf = v.astype(str)
                if exact:
                    m = sdf.apply(lambda col: col.str.strip().str.lower() == q.lower()
                                  if not case else col.str.strip() == q)
                else:
                    m = sdf.apply(lambda col: col.str.contains(q, case=case,
                                                               regex=False, na=False))
                v = v[m.any(axis=1)]
            if adv_sheet == key and adv_col != "None" and adv_val.strip():
                v = v[v[adv_col].astype(str).str.contains(adv_val.strip(), case=case,
                                                          regex=False, na=False)]
            if not v.empty:
                total += len(v)
                tabs_data.append((key, v.head(limit)))

        if not tabs_data:
            st.warning(f"Nothing found for `{q or adv_val}`.")
        else:
            st.success(f"{total} result(s) across {len(tabs_data)} sheet(s)")
            tabs = st.tabs([f"{k} ({len(v)})" for k, v in tabs_data])
            for t, (k, v) in zip(tabs, tabs_data):
                with t:
                    if k == "ATTACHMENTS":
                        imgs = v[v["FILE TYPE"] == "IMAGE"]
                        others = v[v["FILE TYPE"] != "IMAGE"]
                        if not imgs.empty:
                            st.caption(f"{len(imgs)} image(s)")
                            cols = st.columns(4)
                            for i, (_, r) in enumerate(imgs.iterrows()):
                                with cols[i % 4]:
                                    st.image(r["FILE URL"], width="stretch",
                                             caption=r["FILE NAME"])
                                    st.caption(f"ASN {r['ASN NO']}"
                                              + (f" · Inv {r['INVOICE NUMBER']}"
                                                 if clean(r['INVOICE NUMBER']) else ""))
                                    st.link_button("⬇ Download", r["FILE URL"],
                                                  use_container_width=True,
                                                  key=f"srch_dl_img_{r['ATTACH ID']}")
                        if not others.empty:
                            st.caption(f"{len(others)} other file(s)")
                            for _, r in others.iterrows():
                                c1_, c2_, c3_ = st.columns([3, 1.2, 1])
                                c1_.markdown(f"**{ui.esc(r['FILE NAME'])}**  "
                                            f"· ASN {ui.esc(r['ASN NO'])}")
                                c2_.markdown(ui.badge(r["FILE TYPE"], "info"),
                                            unsafe_allow_html=True)
                                attachment_download(c3_, r, "srch_dl_other")
                        continue

                    if q:
                        hit_cols = [c for c in v.columns
                                    if v[c].astype(str).str.contains(
                                        q, case=case, regex=False, na=False).any()]
                        if hit_cols:
                            st.caption("Matched in: " +
                                       ", ".join(f"`{c}`" for c in hit_cols[:10]))
                    show(v, height=460)

            st.download_button(
                "Download the results",
                reporting.build_excel({k[:31]: v for k, v in tabs_data}),
                file_name=f"Search_{date.today():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            asn_hits = set()
            for k, v in tabs_data:
                if "ASN NO" in v.columns:
                    asn_hits |= {clean(a) for a in v["ASN NO"] if clean(a)}
            if asn_hits:
                st.caption("ASNs found: " +
                           ", ".join(f"`{a}`" for a in sorted(asn_hits)[:12]))


# ═══════════════════════════════════════════════════════════════════
#  PENDING LIST
# ═══════════════════════════════════════════════════════════════════
elif page == "Pending List":
    hero("Pending List",
         "Every GRN held up at Korber or AX, with the reason and a remark", "◔")

    if not pipeline.pending_enabled():
        ui.note("This page needs the PENDING sheet, which the deployed "
                "schema.py does not define. Upload every .py file from the "
                "latest package, restart the app, then use Setup - Sheets to "
                "create it.",
                "Files are from different releases", "danger")
        st.stop()

    pend = gsheets.get_df("PENDING")
    summ = gsheets.get_df("ASN_SUMMARY")
    asn_opts = sorted({clean(a) for a in summ["ASN NO"] if clean(a)}) \
        if not summ.empty else []

    openp = pend[pend["STATUS"].astype(str).str.upper() != schema.P_CLEARED] \
        if not pend.empty else pend
    k_open = int((openp["STAGE"] == schema.STAGE_KORBER).sum()) if not openp.empty else 0
    a_open = int((openp["STAGE"] == schema.STAGE_AX).sum()) if not openp.empty else 0
    no_remark = int((openp["REMARK"].astype(str).str.strip() == "").sum()) \
        if not openp.empty else 0

    a, b, c, d = st.columns(4)
    kpi(a, len(openp), "Open holds", WARN if len(openp) else OK)
    kpi(b, k_open, "Korber GRN pending", WARN)
    kpi(c, a_open, "AX GRN pending", INFO)
    kpi(d, no_remark, "Without a remark", DANGER if no_remark else OK,
        "add a reason so the list stays useful" if no_remark else "all annotated")

    ui.section("Raise or update a hold",
               "The reconciliation keeps this list current on its own. Add a "
               "remark here so the reason is recorded against the ASN.")
    with st.form("raise_pending"):
        c1, c2 = st.columns(2)
        r_asn = c1.selectbox("ASN", asn_opts or ["—"])
        r_stage = c2.selectbox("Stage", [schema.STAGE_KORBER, schema.STAGE_AX])
        c1, c2 = st.columns(2)
        reasons = schema.PENDING_REASONS.get(r_stage, ["Other"])
        r_reason = c1.selectbox("Reason", reasons)
        r_prio = c2.selectbox("Priority", ["Normal", "High", "Low"])
        r_remark = st.text_area("Remark", height=80,
                                placeholder="What is holding it up and what "
                                            "happens next")
        c1, c2 = st.columns(2)
        r_follow = c1.text_input("Follow up with", placeholder="Person or team")
        r_note = c2.text_input("Note")
        if st.form_submit_button("Save the hold", type="primary"):
            if r_asn == "—":
                st.error("Pick an ASN first.")
            else:
                pipeline.raise_pending(
                    r_asn, r_stage, r_reason, r_remark, r_prio,
                    SS["user"] or "unknown", r_follow, r_note)
                st.success(f"{r_asn} recorded as pending at {r_stage}.")
                st.rerun()

    ui.section("The register")
    c1, c2, c3 = st.columns(3)
    f_stage = c1.multiselect("Stage", [schema.STAGE_KORBER, schema.STAGE_AX])
    f_stat = c2.multiselect("Status", [schema.P_OPEN, schema.P_CLEARED],
                            default=[schema.P_OPEN])
    f_txt = c3.text_input("Search an ASN or reason")

    v = pend.copy()
    if f_stage:
        v = v[v["STAGE"].isin(f_stage)]
    if f_stat:
        v = v[v["STATUS"].isin(f_stat)]
    if f_txt.strip():
        q = f_txt.strip()
        v = v[v.apply(lambda r: q.lower() in " ".join(
            str(x).lower() for x in r.values), axis=1)]

    show(pick(v, ["ASN NO", "STAGE", "REASON", "REMARK", "PRIORITY",
                  "RAISED AT", "RAISED BY", "FOLLOW UP", "STATUS",
                  "CLEARED AT", "CLEARED BY", "NOTE"]),
         empty_msg="Nothing is on hold.")

    if not v.empty:
        c1, c2 = st.columns(2)
        with c1:
            with st.expander("Clear a hold"):
                ids = st.multiselect("Pending id", list(v["PENDING ID"]))
                cnote = st.text_input("Closing note", key="clear_note")
                if st.button("Clear", disabled=not ids):
                    pipeline.clear_pending(ids, SS["user"] or "unknown", cnote)
                    st.success(f"{len(ids)} cleared.")
                    st.rerun()
        with c2:
            st.download_button(
                "Download the pending list",
                reporting.build_excel({"Pending": v}),
                file_name=f"Pending_{date.today():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch")

    # ── issue resolved? re-check and move it into the AX GRN queue ──
    korber_open = openp[openp["STAGE"] == schema.STAGE_KORBER] \
        if not openp.empty else openp
    if not korber_open.empty:
        with st.expander(
            f"🔁 Re-check and move to AX GRN ({len(korber_open)} held at "
            "Körber GRN)", expanded=False):
            st.caption(
                "Re-runs reconciliation for the ASN(s) you pick against the "
                "current Körber inventory. If every line now matches, the "
                "hold is cleared and the ASN moves into the AX GRN queue "
                "on its own - nothing is forced through.")
            recheck_asns = st.multiselect(
                "ASN No", sorted(korber_open["ASN NO"].astype(str).unique()),
                key="recheck_asns")
            if st.button("Re-check selected ASN(s)", type="primary",
                        disabled=not recheck_asns, key="recheck_btn"):
                with st.spinner("Reconciling..."):
                    moved, still_held = recheck_and_promote(
                        recheck_asns, SS["user"] or "unknown")
                if moved:
                    ui.celebrate(f"{len(moved)} ASN(s) moved to AX GRN",
                                ", ".join(moved))
                if still_held:
                    still_reasons = pipeline.pending_remarks()
                    lines = [f"- **{a}**: {still_reasons.get(clean(a), 'still short of a full match')}"
                             for a in still_held]
                    st.warning(
                        f"**{len(still_held)} ASN(s) not moved** — still not "
                        "fully matched, so the hold stays open:\n\n"
                        + "\n".join(lines))
                if moved or still_held:
                    st.rerun()

    ui.section("Finalize summary report",
               "Pending, discrepancies and completed ASNs in one workbook.")
    st.download_button(
        "Download the finalize report", finalize_bytes(),
        file_name=f"Finalize_Summary_{date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary")


# ═══════════════════════════════════════════════════════════════════
#  DISCREPANCIES
# ═══════════════════════════════════════════════════════════════════
elif page == "Discrepancies":
    hero("Discrepancies", "Everything that did not tally, summarised and by line", "!")

    disc = gsheets.get_df("DISCREPANCY")
    if disc.empty:
        ui.empty("✓", "No discrepancies on record",
                 "Every reconciled line has tallied against the inventory.")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    f_run = c1.selectbox("Reconciliation run", ["All runs"] +
                         sorted({r for r in disc["RUN ID"] if r}, reverse=True))
    f_type = c2.multiselect("Type", sorted({t for t in disc["DISCREPANCY TYPE"] if t}))
    f_sev = c3.multiselect("Severity", sorted({s for s in disc["SEVERITY"] if s}))
    statuses = sorted({s for s in disc["STATUS"] if s})
    f_st = c4.multiselect("Status", statuses,
                          default=[schema.D_OPEN] if schema.D_OPEN in statuses else [])

    v = disc.copy()
    if f_run != "All runs":
        v = v[v["RUN ID"] == f_run]
    if f_type:
        v = v[v["DISCREPANCY TYPE"].isin(f_type)]
    if f_sev:
        v = v[v["SEVERITY"].isin(f_sev)]
    if f_st:
        v = v[v["STATUS"].isin(f_st)]

    a, b, c, d = st.columns(4)
    kpi(a, len(v), "Discrepancy lines", DANGER)
    kpi(b, v["ASN NO"].nunique(), "ASNs affected")
    kpi(c, int((v["SEVERITY"] == "HIGH").sum()), "High severity", DANGER)
    kpi(d, int((disc["STATUS"] == schema.D_RESOLVED).sum()),
        "Auto-resolved to date", OK)

    st.markdown("##### Summary")
    g = (v.assign(_a=v["ASN QTY"].map(to_num), _i=v["INV QTY"].map(to_num))
           .groupby(["ASN NO", "DISCREPANCY TYPE"])
           .agg(Lines=("DISC ID", "count"), ASN_Qty=("_a", "sum"),
                INV_Qty=("_i", "sum")).reset_index())
    g["Qty_Diff"] = g["INV_Qty"] - g["ASN_Qty"]
    show(g)

    st.markdown("##### Line details")
    dcols = ["ASN NO", "ASN LINE", "HU ID", "ITEM NUMBER", "LOT NUMBER", "ASN QTY",
             "INV QTY", "QTY DIFF", "DISCREPANCY TYPE", "SEVERITY", "DETAIL",
             "STATUS", "GENERATED AT", "RUN ID"]
    show(pick(v, dcols))

    st.download_button(
        "Download the discrepancy report",
        reporting.build_excel({"Summary": g, "Details": pick(v, dcols)}),
        file_name=f"Discrepancy_{date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary")

    st.caption("Discrepancies close themselves when a later inventory upload makes "
               "the line tally. Close one here only when it is settled another way.")
    with st.expander("Close manually"):
        ids = st.multiselect("Discrepancy id", list(v["DISC ID"]))
        note = st.text_input("Note")
        if st.button("Close", disabled=not ids):
            full = gsheets.get_df("DISCREPANCY")
            m = full["DISC ID"].isin(ids)
            full.loc[m, "STATUS"] = schema.D_CLOSED
            full.loc[m, "ACTION BY"] = SS["user"] or "unknown"
            full.loc[m, "CLOSED AT"] = now_str()
            full.loc[m, "NOTE"] = note
            gsheets.overwrite("DISCREPANCY", full)
            st.success(f"{len(ids)} closed.")
            st.rerun()


# ═══════════════════════════════════════════════════════════════════
#  ATTACHMENTS  — every image / PDF uploaded against an ASN, in one
#  searchable, downloadable place (Cloudflare R2 behind the scenes)
# ═══════════════════════════════════════════════════════════════════
elif page == "Attachments":
    hero("Attachments",
         "Every photo and PDF uploaded against an ASN — find one by "
         "ASN No, Invoice Number or file name, then download it", "📎")

    if not storage.enabled():
        ui.empty("📎", "Cloudflare R2 is not connected",
                 "Add your R2 account id, keys and bucket to "
                 ".streamlit/secrets.toml (see secrets.toml.example) to "
                 "turn attachments on. Nothing else in the app is affected.")
        st.stop()

    att = gsheets.get_df("ATTACHMENTS")
    summ = gsheets.get_df("ASN_SUMMARY")

    # ── ASNs with no attachment at all — add one right here ──
    att_asns = set(att["ASN NO"].astype(str).map(clean)) if not att.empty else set()
    all_asns = sorted({clean(a) for a in summ["ASN NO"].astype(str) if clean(a)}) \
        if not summ.empty else []
    missing_asns = [a for a in all_asns if a not in att_asns]

    ui.section("ASNs without an attachment",
              f"{len(missing_asns)} ASN(s) have no photo, PDF or Excel file "
              "on file yet" if missing_asns else
              "Every ASN has at least one attachment", 1)
    if missing_asns:
        with st.expander(f"⚠ {len(missing_asns)} ASN(s) missing an attachment "
                         "— add one here", expanded=att.empty):
            mc1, mc2 = st.columns([1, 2])
            with mc1:
                pick_asns = st.multiselect(
                    "ASN No — select one or more", missing_asns,
                    key="missing_asn_pick",
                    help="Pick several ASNs to link them all to the same "
                         "document(s) you upload below.")
                inv_opts = sorted({
                    clean(part)
                    for s in att["INVOICE NUMBER"].astype(str)
                    for part in s.split(",")
                    if clean(part)
                }) if not att.empty else []
                pick_inv_sel = st.multiselect(
                    "Invoice Number(s) — pick existing", inv_opts,
                    key="missing_asn_inv_sel",
                    help="Pick one or more invoices already on file to link to "
                         "this document.") if inv_opts else []
                pick_inv_new = st.text_input(
                    "Add invoice number(s)", key="missing_asn_inv_new",
                    placeholder="e.g. INV-1001, INV-1002",
                    help="Type one or more new invoice numbers separated by "
                         "commas — all of them link to this one document.")
            with mc2:
                pick_files = st.file_uploader(
                    "Photos, scanned invoice, delivery note, packing list, etc.",
                    type=["jpg", "jpeg", "png", "webp", "pdf", "xlsx", "xls", "xlsm"],
                    accept_multiple_files=True, key="missing_asn_files")
            if st.button("Upload attachment(s)", type="primary",
                        disabled=not (pick_files and pick_asns),
                        key="missing_asn_upload"):
                ts, user = now_str(), SS["user"] or "unknown"
                inv_all = list(pick_inv_sel) + \
                    pick_inv_new.replace("\n", ",").split(",")
                inv_joined = ", ".join(dict.fromkeys(
                    clean(x) for x in inv_all if clean(x)))
                up_rows, failed = [], []
                for f in pick_files:
                    b = f.getvalue()
                    ftype = attach_file_type(f.name)
                    try:
                        # Upload the file once, then link it to every ASN picked.
                        key = storage.object_key(pick_asns[0], f.name)
                        if ftype in ("PDF", "EXCEL"):
                            url, _ = storage.upload_compressed(b, key, f.type)
                        else:
                            url = storage.upload(b, key, f.type)
                    except Exception as e:
                        failed.append(f"{f.name}: {e}")
                        continue
                    for asn in pick_asns:
                        up_rows.append({
                            "ATTACH ID": uuid.uuid4().hex[:10].upper(),
                            "ASN NO": asn, "INVOICE NUMBER": inv_joined,
                            "FILE NAME": f.name, "FILE TYPE": ftype, "FILE URL": url,
                            "SIZE KB": round(len(b) / 1024, 1),
                            "UPLOADED AT": ts, "UPLOADED BY": user, "NOTE": "",
                        })
                if up_rows:
                    gsheets.upsert("ATTACHMENTS", up_rows)
                    ui.celebrate(f"{len(up_rows)} attachment link(s) added",
                                f"{len(pick_files)} file(s) × {len(pick_asns)} ASN(s)")
                if failed:
                    st.error("Some files failed to upload:\n\n"
                             + "\n".join(f"- {x}" for x in failed))
                if up_rows:
                    st.rerun()

    if att.empty:
        ui.empty("📎", "No attachments yet",
                 "Pick an ASN above to add the first one.")
        st.stop()

    a, b, c = st.columns(3)
    kpi(a, len(att), "Files")
    kpi(b, att["ASN NO"].astype(str).str.strip().nunique(), "ASN No covered")
    kpi(c, fmt_num(att["SIZE KB"].map(to_num).sum() / 1024), "Total size (MB)")

    ui.section("Find a file", "Search by ASN No, Invoice Number or file name.", 2)
    q1, q2, q3 = st.columns([2, 1, 1])
    q = q1.text_input("Search", placeholder="e.g. ASN-10234, INV-9981 or photo.jpg",
                      label_visibility="collapsed")
    type_f = q2.selectbox("Type", ["All types", "IMAGE", "PDF", "EXCEL"])
    sort_new = q3.selectbox("Sort", ["Newest first", "Oldest first"])

    view = att.copy()
    if q.strip():
        qn = nkey(q)
        ql = q.strip().lower()
        view = view[
            view["ASN NO"].astype(str).map(nkey).eq(qn)
            | view["INVOICE NUMBER"].astype(str).map(
                lambda s: qn in {nkey(p) for p in str(s).split(",")})
            | view["FILE NAME"].astype(str).str.lower().str.contains(ql, regex=False)
        ]
    if type_f != "All types":
        view = view[view["FILE TYPE"] == type_f]
    view = view.sort_values("UPLOADED AT", ascending=(sort_new == "Oldest first"))

    st.caption(f"{len(view)} of {len(att)} file(s)")

    if view.empty:
        st.info("No attachments match that search.")
    else:
        is_admin = SS["role"] == "admin"
        for _, r in view.head(60).iterrows():
            with st.container(border=True):
                cimg, cinfo, cdl = st.columns([1, 3, 1.1])
                with cimg:
                    if r["FILE TYPE"] == "IMAGE":
                        st.image(r["FILE URL"], width="stretch")
                    else:
                        badge_color = {"PDF": DANGER, "EXCEL": OK}.get(
                            r["FILE TYPE"], MUTED)
                        st.markdown(
                            f"<div style='background:{LINE};border-radius:8px;"
                            f"height:80px;display:flex;align-items:center;"
                            f"justify-content:center;color:{badge_color};"
                            f"font-weight:700;font-size:.75rem'>{r['FILE TYPE']}</div>",
                            unsafe_allow_html=True)
                with cinfo:
                    st.markdown(f"**{ui.esc(r['FILE NAME'])}**")
                    st.caption(
                        f"ASN {r['ASN NO']}"
                        + (f" · Invoice {r['INVOICE NUMBER']}"
                           if clean(r['INVOICE NUMBER']) else "")
                        + f" · {r['SIZE KB']} KB · {r['UPLOADED AT']}"
                        + (f" · by {r['UPLOADED BY']}" if clean(r['UPLOADED BY']) else ""))
                with cdl:
                    attachment_download(cdl, r, "attpage")
                    if is_admin:
                        if st.button("🗑 Delete", key=f"del_att_{r['ATTACH ID']}",
                                    use_container_width=True):
                            try:
                                storage.delete(storage.url_to_key(r["FILE URL"]))
                            except Exception:
                                pass
                            full = gsheets.get_df("ATTACHMENTS")
                            full = full[full["ATTACH ID"] != r["ATTACH ID"]]
                            gsheets.overwrite("ATTACHMENTS", full)
                            st.rerun()
        if len(view) > 60:
            st.caption(f"Showing the first 60 of {len(view)} — narrow your search to see more.")


# ═══════════════════════════════════════════════════════════════════
#  EMAIL
# ═══════════════════════════════════════════════════════════════════
elif page == "Email":
    hero("Discrepancy Email", "Detailed Markdown, ready to copy into your mail client", "✉")

    disc = gsheets.get_df("DISCREPANCY")
    summ = gsheets.get_df("ASN_SUMMARY")
    s = gsheets.settings_dict()

    log = gsheets.get_df("EMAIL_LOG")
    if not log.empty:
        with st.expander(f"Previously generated emails ({len(log)})"):
            show(pick(log, ["EMAIL ID", "GENERATED AT", "GENERATED BY", "ASN LIST",
                      "SUBJECT"]))
            pick_id = st.selectbox("Reopen", ["None"] + list(log["EMAIL ID"]))
            if pick_id != "None":
                row = log[log["EMAIL ID"] == pick_id].iloc[0]
                st.code(row["SUBJECT"], language="text")
                st.code(row["BODY MD"], language="markdown")

    if disc.empty:
        ui.empty("✉", "Nothing to report",
                 "No discrepancies are on record, so no email is needed.")
        st.stop()

    c1, c2 = st.columns(2)
    runs = ["All runs"] + sorted({r for r in disc["RUN ID"] if r}, reverse=True)
    f_run = c1.selectbox("Reconciliation run", runs)
    only_open = c2.checkbox("Open items only", value=True)

    v = disc.copy()
    if f_run != "All runs":
        v = v[v["RUN ID"] == f_run]
    if only_open:
        v = v[v["STATUS"] == schema.D_OPEN]

    asn_opts = sorted({a for a in v["ASN NO"] if a})
    picked = st.multiselect("ASNs", asn_opts, default=asn_opts)
    v = v[v["ASN NO"].isin(picked)]

    c1, c2 = st.columns(2)
    to = c1.text_input("To", s.get("EMAIL_TO", ""))
    cc = c2.text_input("Cc", s.get("EMAIL_CC", ""))

    if st.button("Generate", type="primary", disabled=v.empty):
        sm = summ[summ["ASN NO"].isin(picked)] if not summ.empty else pd.DataFrame()
        subject, md = reporting.discrepancy_email(
            sm, v, company=s.get("COMPANY", "EFL"), site=s.get("SITE", ""),
            client=s.get("CLIENT_CODE", ""), prepared_by=SS["user"] or "",
            to=to, cc=cc, run_id="" if f_run == "All runs" else f_run,
            inventory_note=SS.get("inv_note", ""))
        SS["email"] = {"subject": subject, "md": md, "asns": picked,
                       "to": to, "cc": cc}

    E = SS.get("email")
    if E:
        st.markdown("##### Subject")
        st.code(E["subject"], language="text")
        st.markdown("##### Body")
        st.code(E["md"], language="markdown")

        c1, c2 = st.columns(2)
        c1.download_button("Download as .md", E["md"].encode("utf-8"),
                           file_name=f"discrepancy_email_{date.today():%Y%m%d}.md",
                           mime="text/markdown")
        if c2.button("Save to EMAIL_LOG"):
            gsheets.append_rows("EMAIL_LOG", [[
                uuid.uuid4().hex[:10].upper(), now_str(), SS["user"] or "unknown",
                ", ".join(E["asns"][:20]), E["subject"], E.get("to", ""),
                E.get("cc", ""), E["md"][:45000]]])
            st.success("Saved.")

        with st.expander("Rendered preview"):
            st.markdown(E["md"])


# ═══════════════════════════════════════════════════════════════════
#  AX GRN
# ═══════════════════════════════════════════════════════════════════
elif page == "AX GRN":
    hero("AX GRN",
         "Korber GRN done → AX GRN pending → AX GRN done → fully complete", "✓")

    # ── attachments — quick pointer to the dedicated page ──
    if storage.enabled():
        att_all = gsheets.get_df("ATTACHMENTS")
        n_att = len(att_all)
        st.caption(f"📎 Looking for a photo or scanned document? "
                   f"{n_att} file(s) are on the **Attachments** page "
                   "(Review group) — searchable by ASN No or Invoice Number.")

    ax = gsheets.get_df("AX_GRN")
    if ax.empty:
        ui.empty("✓", "The AX queue is empty",
                 "ASNs arrive here automatically once every line tallies "
                 "against the Korber inventory.")
        st.stop()

    pend = ax[ax["AX GRN"] != schema.AX_DONE]
    done = ax[ax["AX GRN"] == schema.AX_DONE]

    a, b, c = st.columns(3)
    kpi(a, len(pend), "Awaiting AX GRN", INFO)
    kpi(b, len(done), "AX GRN done", OK)
    kpi(c, fmt_num(pend["TOTAL QTY"].map(to_num).sum()), "Quantity pending")

    ui.section("Awaiting AX GRN")
    if pend.empty:
        st.success("Nothing pending.")
    else:
        remarks = pipeline.pending_remarks()
        att_all = gsheets.get_df("ATTACHMENTS") if storage.enabled() else pd.DataFrame()
        att_counts = ({} if att_all.empty else
                      att_all.groupby(att_all["ASN NO"].astype(str).map(clean))
                             .size().to_dict())
        view = pend.copy()
        view["HOLD REASON"] = view["ASN NO"].astype(str).map(
            lambda a_: remarks.get(clean(a_), ""))
        if storage.enabled():
            view["📎 FILES"] = view["ASN NO"].astype(str).map(
                lambda a_: att_counts.get(clean(a_), 0))
        cols = ["ASN NO", "CLIENT CODE", "KORBER GRN NO",
                "KORBER GRN DATE", "TOTAL LINES", "TOTAL QTY",
                "OVERRIDE", "HOLD REASON", "OVERRIDE REASON", "REMARK",
                "PUSHED AT", "PUSHED BY"]
        if storage.enabled():
            cols.insert(1, "📎 FILES")
        show(pick(view, cols))

        if pipeline.pending_enabled():
            with st.expander("🚩 Flag an AX System issue", expanded=False):
                st.caption(
                    "For an ASN stuck here because of a problem on the AX "
                    "side (interface error, master data, approval, etc.) "
                    "rather than a data issue. It shows up as the Hold "
                    "Reason above, and clears itself the moment you mark "
                    "that ASN's AX GRN done below — no separate step needed.")
                ax_reasons = schema.PENDING_REASONS[schema.STAGE_AX]
                fc1, fc2 = st.columns([1.3, 2])
                flag_asns = fc1.multiselect(
                    "ASN No", list(pend["ASN NO"].astype(str)), key="ax_flag_asns")
                flag_reason = fc1.selectbox(
                    "Reason", ax_reasons,
                    index=ax_reasons.index("Interface error"), key="ax_flag_reason")
                flag_remark = fc2.text_area(
                    "Remark", height=80, key="ax_flag_remark",
                    placeholder="What's wrong on the AX side, and who's fixing it")
                if st.button("Flag selected ASN(s)", disabled=not flag_asns,
                            key="ax_flag_btn"):
                    for a in flag_asns:
                        pipeline.raise_pending(
                            a, schema.STAGE_AX, flag_reason, flag_remark,
                            "High", SS["user"] or "unknown")
                    st.success(f"{len(flag_asns)} ASN(s) flagged with an AX "
                              "System issue.")
                    st.rerun()

        ui.section("Mark as done in AX")
        c1, c2, c3 = st.columns([2, 1, 1])
        sel = c1.multiselect("ASNs", list(pend["ASN NO"].astype(str)))
        ax_no = c2.text_input("AX GRN number", "")
        ax_dt = c3.date_input("AX GRN date", value=date.today())

        # ── the invoice attachment(s) for whichever ASN(s) are picked
        #    above, right where the AX GRN is actually being updated ──
        if sel and storage.enabled():
            sel_set = {clean(s) for s in sel}
            hit = (att_all[att_all["ASN NO"].astype(str).map(clean).isin(sel_set)]
                  if not att_all.empty else att_all)
            with st.expander(
                f"📎 {len(hit)} attachment(s) for the selected ASN(s)",
                expanded=bool(len(hit))):
                if hit.empty:
                    st.caption("No photos, PDFs or Excel files on file for "
                               "these ASN(s) — see the Attachments page to upload one.")
                else:
                    for _, r in hit.iterrows():
                        ca, cb, cc = st.columns([2.6, 1, 1])
                        ca.markdown(
                            f"**{ui.esc(r['FILE NAME'])}**  \n"
                            f"<span style='color:{MUTED};font-size:.78rem'>"
                            f"ASN {ui.esc(r['ASN NO'])}"
                            + (f" · Invoice {ui.esc(r['INVOICE NUMBER'])}"
                               if clean(r['INVOICE NUMBER']) else "")
                            + f" · {ui.esc(r['SIZE KB'])} KB</span>",
                            unsafe_allow_html=True)
                        cb.markdown(ui.badge(r["FILE TYPE"], "info"),
                                   unsafe_allow_html=True)
                        attachment_download(cc, r, "ax_att_dl")

        if st.button("Mark AX GRN done", type="primary", disabled=not sel):
            ts, user = now_str(), SS["user"] or "unknown"
            m = ax["ASN NO"].astype(str).isin(sel)
            ax.loc[m, "AX GRN"] = schema.AX_DONE
            ax.loc[m, "AX GRN NO"] = ax_no
            ax.loc[m, "AX GRN DATE"] = str(ax_dt)
            ax.loc[m, "AX GRN BY"] = user
            ax.loc[m, "OVERALL"] = schema.S_COMPLETE
            gsheets.overwrite("AX_GRN", ax)

            summ = gsheets.get_df("ASN_SUMMARY")
            ms = summ["ASN NO"].astype(str).isin(sel)
            summ.loc[ms, "AX GRN"] = schema.AX_DONE
            summ.loc[ms, "AX GRN NO"] = ax_no
            summ.loc[ms, "AX GRN DATE"] = str(ax_dt)
            summ.loc[ms, "AX GRN BY"] = user
            summ.loc[ms, "OVERALL"] = schema.S_COMPLETE
            summ.loc[ms, "STATUS"] = schema.S_COMPLETE
            gsheets.overwrite("ASN_SUMMARY", summ)

            det = gsheets.get_df("ASN_DETAIL")
            md = det["ASN NO"].astype(str).isin(sel)
            det.loc[md, "AX GRN"] = schema.AX_DONE
            det.loc[md, "REMARK"] = f"AX GRN done {ts}"
            gsheets.overwrite("ASN_DETAIL", det)

            pipeline.clear_stage(sel, schema.STAGE_AX, user)

            st.success(f"{len(sel)} ASN(s) are now fully complete.")
            ui.celebrate(f"{len(sel)} ASN(s) completed", "Fully reconciled and posted to AX")
            st.rerun()

    ui.section("Send to AX despite a discrepancy",
               "For ASNs that still carry a discrepancy but have to be posted. "
               "The override, the reason and your remark are all recorded.")

    if not pipeline.pending_enabled():
        ui.note("This needs the PENDING sheet from the latest schema.py. "
                "Upload every .py file from the package and restart.",
                "Not available in this build", "warn")
        blocked = []
        summ_all = pd.DataFrame()
    else:
        summ_all = gsheets.get_df("ASN_SUMMARY")
    already = set(ax["ASN NO"].astype(str)) if not ax.empty else set()
    if not summ_all.empty:
        m = ((summ_all["OVERALL"] != schema.S_COMPLETE)
             & (~summ_all["ASN NO"].astype(str).isin(already)))
        blocked = sorted({clean(a) for a in summ_all.loc[m, "ASN NO"] if clean(a)})

    if not blocked:
        st.caption("Every ASN with an outstanding issue is already in the queue.")
    else:
        disc_all = gsheets.get_df("DISCREPANCY")
        open_counts = {}
        if not disc_all.empty:
            o = disc_all[disc_all["STATUS"].astype(str).str.upper() == schema.D_OPEN]
            open_counts = o.groupby(o["ASN NO"].astype(str)).size().to_dict()

        with st.expander(f"🔁 Re-check first ({len(blocked)} blocked) — "
                         "no override needed if it now tallies", expanded=True):
            st.caption(
                "Re-runs reconciliation against the current Körber inventory. "
                "Whatever now matches is pushed to AX GRN on its own, with no "
                "override recorded. Only what's still short needs the form below.")
            recheck_sel = st.multiselect(
                "ASNs to re-check", blocked, key="ax_recheck_asns",
                format_func=lambda a: (f"{a} — {open_counts.get(a, 0)} open "
                                       f"discrepancy line(s)"))
            if st.button("Re-check selected ASN(s)", key="ax_recheck_btn",
                        disabled=not recheck_sel):
                with st.spinner("Reconciling..."):
                    moved, still_held = recheck_and_promote(
                        recheck_sel, SS["user"] or "unknown")
                if moved:
                    ui.celebrate(f"{len(moved)} ASN(s) moved to AX GRN",
                                ", ".join(moved))
                if still_held:
                    st.warning(f"**{len(still_held)} ASN(s) still blocked** — "
                              "use the form below to send with an override, "
                              "or resolve the discrepancy first.")
                if moved or still_held:
                    st.rerun()

        with st.form("override_push"):
            sel_o = st.multiselect(
                "ASNs to send", blocked, key="ov_asns",
                format_func=lambda a: (f"{a} — {open_counts.get(a, 0)} open "
                                       f"discrepancy line(s)"))
            c1, c2 = st.columns([1, 2])
            o_reason = c1.selectbox("Reason",
                                    schema.PENDING_REASONS[schema.STAGE_AX],
                                    key="ov_reason")
            o_remark = c2.text_area(
                "Remark (required)", height=80, key="ov_remark",
                placeholder="Why this is being posted with the variance, and "
                            "who approved it")
            ack = st.checkbox("I confirm this ASN may be posted with its "
                              "discrepancy outstanding.", key="ov_ack")
            go = st.form_submit_button("Send to AX GRN Pending", type="primary")

        if go:
            if not sel_o:
                st.error("Pick at least one ASN.")
            elif not o_remark.strip():
                st.error("A remark is required for an override.")
            elif not ack:
                st.error("Tick the confirmation first.")
            else:
                res = pipeline.push_to_ax(sel_o, SS["user"] or "unknown",
                                          o_reason, o_remark.strip(), True)
                st.success(f"{res['pushed']} ASN(s) sent to AX GRN Pending with "
                           f"an override, and added to the pending list.")
                st.rerun()

    ui.section("Fully complete")
    show(done)

    c1, c2 = st.columns(2)
    c1.download_button(
        "Download the AX queue",
        reporting.build_excel({"Pending": pend, "Completed": done}),
        file_name=f"AX_GRN_{date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch")
    c2.download_button(
        "Download the finalize report", finalize_bytes(),
        file_name=f"Finalize_Summary_{date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch")


# ═══════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════
elif page == "Dashboard":
    summ = gsheets.get_df("ASN_SUMMARY")
    det = gsheets.get_df("ASN_DETAIL")
    disc = gsheets.get_df("DISCREPANCY")
    log = gsheets.get_df("RECON_LOG")

    last = clean(log["RUN AT"].iloc[-1]) if not log.empty else "not yet run"
    hero("Dashboard", f"ASN → Körber GRN → AX GRN · Last reconciliation {last}", "📊")

    if summ.empty:
        ui.empty("◧", "Nothing to show yet",
                 "Upload an ASN document, then upload the Korber inventory. "
                 "Reconciliation runs on its own from there.")
        st.stop()

    n_asn = len(summ)
    n_korber = int((summ["KORBER GRN"] == schema.K_DONE).sum())
    n_axp = int((summ["AX GRN"] == schema.AX_PENDING).sum())
    n_comp = int((summ["OVERALL"] == schema.S_COMPLETE).sum())
    n_open = int((disc["STATUS"] == schema.D_OPEN).sum()) if not disc.empty else 0
    n_res = int((disc["STATUS"] == schema.D_RESOLVED).sum()) if not disc.empty else 0
    asn_qty = summ["TOTAL QTY"].map(to_num).sum()
    rec_qty = summ["RECEIVED QTY"].map(to_num).sum()
    lines = len(det)
    tally = int((det["MATCH STATUS"] == schema.M_MATCHED).sum()) if not det.empty else 0
    rate = (tally / lines * 100) if lines else 0
    pending = n_asn - n_comp
    variance = rec_qty - asn_qty
    holds = pipeline.open_pending()

    # ── one clean row of the numbers that actually matter ──
    a, b, c, d, e = st.columns(5)
    kpi(a, n_asn, "Total ASNs", note=f"{lines} lines")
    kpi(b, f"{rate:.0f}%", "Match Rate", ACCENT, f"{tally}/{lines} lines")
    kpi(c, n_open, "Open Issues", DANGER if n_open else OK, f"{n_res} resolved")
    kpi(d, pending, "Pending", WARN if pending else OK, f"{n_comp} complete")
    kpi(e, fmt_num(variance), "Qty Variance",
        OK if abs(variance) < 1 else WARN,
        f"{(variance/asn_qty*100):+.1f}%" if asn_qty else "")

    # ── pipeline — the one visual the dashboard needs ──
    st.markdown("")
    ui.section("Processing Pipeline", "Movement through each stage of the GRN process")
    pipeline_strip([
        ("ASN Uploaded", n_asn, MUTED),
        ("Körber GRN Done", n_korber, ACCENT),
        ("AX GRN Pending", n_axp, INFO),
        ("Fully Complete", n_comp, OK),
    ])

    # ── requires action — the one table worth surfacing here ──
    st.markdown("")
    need = summ[summ["OVERALL"] != schema.S_COMPLETE]
    ui.section("Requires Action",
               "ASNs that are not yet fully complete" if not need.empty
               else "Every ASN is fully complete", 2)
    if need.empty:
        st.success("✅ Nothing outstanding — all ASNs are fully complete.")
    else:
        show(pick(need, ["ASN NO", "TOTAL LINES", "MATCHED LINES", "MISSING LINES",
                   "MISMATCH LINES", "EXTRA LINES", "STATUS", "KORBER GRN",
                   "AX GRN", "LAST RECON"]), height=260)
    if not holds.empty:
        st.caption(f"⚠ {len(holds)} open hold(s) in the pending register — "
                   "see the Pending List page.")
    ax_all = gsheets.get_df("AX_GRN")
    overridden = (ax_all[(ax_all["OVERRIDE"] == "Y") & (ax_all["AX GRN"] != schema.AX_DONE)]
                 if not ax_all.empty and "OVERRIDE" in ax_all.columns else pd.DataFrame())
    if not overridden.empty:
        st.caption(f"⚠ {len(overridden)} ASN(s) sent to AX GRN with an "
                   "outstanding discrepancy (override) — see the AX GRN page.")

    # ── report export — a utility, tucked away so it doesn't compete
    #    with the numbers above for attention ──
    with st.expander("📥 Export a summary report", expanded=False):
        c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1.6])
        start_date = c1.date_input("From", value=date.today().replace(day=1),
                                   key="report_start_date")
        end_date = c2.date_input("To", value=date.today(),
                                 key="report_end_date")
        c3.write("")
        c3.write("")
        if c3.button("Reset", key="reset_dates", use_container_width=True):
            st.session_state.report_start_date = date.today().replace(day=1)
            st.session_state.report_end_date = date.today()
            st.rerun()
        c4.write("")
        c4.write("")
        if start_date <= end_date:
            c4.download_button(
                "Download report", finalize_bytes(start_date, end_date),
                file_name=f"Finalize_Summary_{start_date:%Y%m%d}_{end_date:%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary", use_container_width=True)
        else:
            c4.error("From date is after To date")


# ═══════════════════════════════════════════════════════════════════
#  DATA MANAGER
# ═══════════════════════════════════════════════════════════════════
elif page == "Data Manager":
    hero("Data Manager", "Edit any sheet directly (admin only)", "▦")

    if SS["role"] != "admin":
        st.warning("Admin only. Sign in from the sidebar.")
        st.stop()

    key = st.selectbox("Sheet", list(schema.SHEETS))
    df = gsheets.get_df(key)
    st.caption(f"{len(df)} rows · {len(schema.SHEETS[key]['headers'])} columns")

    ed = st.data_editor(df, num_rows="dynamic", width="stretch",
                        height=520, key=f"ed_{key}")

    c1, c2 = st.columns([1, 3])
    if c1.button("Save", type="primary"):
        gsheets.overwrite(key, ed)
        st.success("Saved.")
        st.rerun()
    c2.caption("Saving replaces the whole sheet with what is shown here.")

    st.download_button(f"Download {key}",
                       reporting.build_excel({key[:31]: df}),
                       file_name=f"{key}_{date.today():%Y%m%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ═══════════════════════════════════════════════════════════════════
#  MAINTENANCE
# ═══════════════════════════════════════════════════════════════════
elif page == "Maintenance":
    hero("Maintenance", "Delete an ASN · clear a sheet · reset the database", "⚑")

    if SS["role"] != "admin":
        st.warning("Admin only. Sign in from the sidebar.")
        st.stop()

    ASN_SHEETS = {"ASN_SUMMARY": "ASN NO", "ASN_DETAIL": "ASN NO",
                  "DISCREPANCY": "ASN NO", "AX_GRN": "ASN NO",
                  "ATTACHMENTS": "ASN NO"}

    t1, t2, t3 = st.tabs(["Delete an ASN", "Clear a sheet", "Reset the database"])

    with t1:
        st.caption("Removes the summary, line details, discrepancies, AX GRN entry "
                   "for the selected ASNs. This cannot be undone.")

        summ = gsheets.get_df("ASN_SUMMARY")
        det = gsheets.get_df("ASN_DETAIL")
        opts = sorted({clean(a) for a in summ["ASN NO"] if clean(a)}) \
            if not summ.empty else []
        if not opts and not det.empty:
            opts = sorted({clean(a) for a in det["ASN NO"] if clean(a)})

        if not opts:
            st.info("No ASN records.")
        else:
            sel = st.multiselect("ASNs to delete", opts, key="del_asn")
            if sel:
                counts = {}
                for k, col in ASN_SHEETS.items():
                    d = gsheets.get_df(k)
                    counts[k] = 0 if d.empty or col not in d.columns else int(
                        d[col].astype(str).str.strip().isin(sel).sum())

                st.markdown("###### What will be removed")
                show(pd.DataFrame([{"Sheet": k, "Rows": v} for k, v in counts.items()]))

                prev = det[det["ASN NO"].astype(str).isin(sel)] \
                    if not det.empty else pd.DataFrame()
                with st.expander(f"Lines to be deleted ({len(prev)})"):
                    show(pick(prev, ["ASN NO", "ASN LINE", "HU ID",
                                     "ITEM NUMBER", "QTY", "MATCH STATUS",
                                     "KORBER GRN", "AX GRN"])
                         if not prev.empty else prev)

                st.download_button(
                    "Back these up first",
                    reporting.build_excel({
                        "Summary": summ[summ["ASN NO"].astype(str).isin(sel)],
                        "Details": prev}),
                    file_name=f"ASN_backup_{date.today():%Y%m%d}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

                typed = st.text_input("Type DELETE to confirm", key="del_confirm")

                if st.button("Delete", type="primary",
                             disabled=typed.strip().upper() != "DELETE"):
                    with st.spinner("Deleting..."):
                        if storage.enabled():
                            att = gsheets.get_df("ATTACHMENTS")
                            if not att.empty:
                                gone = att[att["ASN NO"].astype(str).isin(sel)]
                                for u in gone["FILE URL"]:
                                    try:
                                        storage.delete(storage.url_to_key(u))
                                    except Exception:
                                        pass
                        res = {k: gsheets.delete_where(k, col, sel)
                               for k, col in ASN_SHEETS.items()}
                    st.success("Deleted — " +
                               ", ".join(f"{k}: {v}" for k, v in res.items()))
                    st.rerun()

    with t2:
        st.caption("Removes every data row and keeps the header. The sheet itself "
                   "stays in place.")
        show(gsheets.sheet_status())

        k = st.selectbox("Sheet", list(schema.SHEETS), key="clr_sheet")
        cur = gsheets.get_df(k)
        st.caption(f"{len(cur)} rows at the moment.")

        st.download_button(
            f"Back up {k}", reporting.build_excel({k[:31]: cur}),
            file_name=f"{k}_backup_{date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        typed2 = st.text_input(f"Type {k} to confirm", key="clr_confirm")
        if st.button("Clear", disabled=typed2.strip() != k, type="primary"):
            n = gsheets.clear_sheet(k)
            st.success(f"{k} — {n} rows cleared.")
            st.rerun()

    with t3:
        st.error("This removes every record in the selected sheets and cannot be "
                 "undone. Take a backup first.")

        DATA_SHEETS = ["ASN_SUMMARY", "ASN_DETAIL", "INVENTORY", "DISCREPANCY",
                       "AX_GRN", "PENDING", "ATTACHMENTS", "RECON_LOG",
                       "EMAIL_LOG"]
        MASTERS = ["USER-M", "SETTINGS"]

        scope = st.radio(
            "Scope",
            ["Transaction data only — keeps users and settings",
             "Choose the sheets myself",
             "Everything, including users and settings"],
            key="reset_scope")

        if scope.startswith("Transaction"):
            targets = DATA_SHEETS
        elif scope.startswith("Everything"):
            targets = DATA_SHEETS + MASTERS
        else:
            targets = st.multiselect("Sheets", list(schema.SHEETS),
                                     default=DATA_SHEETS, key="reset_pick")

        rows_now = {}
        for k in targets:
            try:
                rows_now[k] = len(gsheets.get_df(k))
            except Exception:
                rows_now[k] = 0
        st.markdown(f"**{len(targets)} sheet(s) · {sum(rows_now.values())} rows** "
                    f"will be removed")
        show(pd.DataFrame([{"Sheet": k, "Rows": v} for k, v in rows_now.items()]))

        if st.button("Build a backup file", key="mk_backup"):
            SS["backup"] = reporting.build_excel(
                {k[:31]: gsheets.get_df(k) for k in targets})
        if SS.get("backup"):
            st.download_button(
                "Download the backup", SS["backup"],
                file_name=f"FULL_BACKUP_{datetime.now():%Y%m%d_%H%M}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        c1, c2 = st.columns(2)
        pin2 = c1.text_input("Admin PIN again", type="password", key="reset_pin")
        typed3 = c2.text_input("Type RESET to confirm", key="reset_confirm")
        ack = st.checkbox("I understand this data cannot be recovered.",
                          key="reset_ack")

        good_pin = pin2 == str(gsheets.settings_dict().get("ADMIN_PIN", "1234"))
        ready = bool(targets) and good_pin and typed3.strip().upper() == "RESET" and ack
        if pin2 and not good_pin:
            st.warning("Wrong PIN.")

        if st.button("Reset the database", type="primary", disabled=not ready):
            with st.spinner("Resetting..."):
                res = gsheets.reset_database(targets)
                if "SETTINGS" in targets or "USER-M" in targets:
                    gsheets.ensure_all()
            st.success("Reset — " + ", ".join(f"{k}: {v}" for k, v in res.items()))
            SS["auto"] = SS["recon"] = SS["email"] = None
            SS["parsed_asn"] = {}
            SS["inv_df"] = None
            SS["backup"] = None
            st.rerun()


# ───────────────────────────── footer ─────────────────────────────
st.markdown(
    f"<div style='text-align:center;color:{MUTED};font-size:.73rem;"
    f"margin-top:2.4rem;padding-top:1rem;border-top:1px solid {LINE}'>"
    "ASN / GRN Control System · Korber One and AX · EFL Warehouse Operations"
    "</div>", unsafe_allow_html=True)
