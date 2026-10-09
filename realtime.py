"""
realtime.py
===========
A lightweight real-time data engine for the ASN / GRN control system.

Streamlit has no server-push channel to the browser, so "real time" here
means a self-ticking fragment: on the interval you choose it drops the
Google-Sheets cache and triggers a full app rerun. Every page then
repaints with fresh data on its own — no per-page wiring is needed, so
turning Live on affects the whole app at once.

Public API
    init()              set up session defaults (call once, early)
    render_control()    the Live on/off + interval popover (one column)
    live_banner()       render the animated "LIVE" status pill when on
    engine()            the ticking fragment (call once near the end of a run)
"""
from __future__ import annotations

import time
from datetime import datetime

import streamlit as st

import gsheets
import ui

SS = st.session_state

# label -> seconds
INTERVALS = {"5s": 5, "10s": 10, "30s": 30, "60s": 60, "2m": 120}
DEFAULT_INTERVAL = "10s"


def init() -> None:
    """Session defaults. Safe to call on every run."""
    SS.setdefault("rt_live", False)
    SS.setdefault("rt_interval", DEFAULT_INTERVAL)
    SS.setdefault("rt_last_sync", None)
    SS.setdefault("rt_last_tick", None)


def _secs() -> int:
    return INTERVALS.get(SS.get("rt_interval", DEFAULT_INTERVAL), 10)


def render_control() -> None:
    """A popover holding the Live toggle and the refresh interval.

    Drop it into its own column in the top controls row.
    """
    live = SS.get("rt_live", False)
    label = "🟢 Live" if live else "⚪ Live"
    with st.popover(label, use_container_width=True):
        st.toggle(
            "Real-time updates", key="rt_live",
            help="Automatically pull fresh data from Google Sheets on a timer "
                 "and repaint every page. Turn off to stop auto-refreshing.")
        st.selectbox(
            "Refresh every", list(INTERVALS), key="rt_interval",
            help="How often to re-sync while Live is on. Mind the Google API "
                 "limit — very short intervals use more calls.")
        if SS.get("rt_last_sync"):
            st.caption(f"Last synced at {SS['rt_last_sync']}")
        else:
            st.caption("Not synced yet this session.")


def live_banner() -> None:
    """A thin animated status pill, shown just under the controls row while
    Live is on. Pure CSS — the dot pulses, the sheen sweeps."""
    if not SS.get("rt_live"):
        return
    synced = SS.get("rt_last_sync") or "—"
    st.markdown(
        f"""
<style>
  @keyframes rt-pulse {{
    0%   {{ box-shadow:0 0 0 0 {ui.OK}aa; opacity:1; }}
    70%  {{ box-shadow:0 0 0 7px {ui.OK}00; opacity:.75; }}
    100% {{ box-shadow:0 0 0 0 {ui.OK}00; opacity:1; }}
  }}
  @keyframes rt-sheen {{
    0%   {{ transform:translateX(-120%); }}
    100% {{ transform:translateX(320%); }}
  }}
  .rt-pill {{ position:relative; overflow:hidden; display:inline-flex;
      align-items:center; gap:.5rem; margin:-.35rem 0 .7rem 0;
      padding:.32rem .8rem; border-radius:999px;
      background:{ui.SURFACE}; border:1px solid {ui.OK}55;
      font-size:.74rem; font-weight:620; color:{ui.INK_2};
      letter-spacing:.01em; }}
  .rt-pill .dot {{ width:8px; height:8px; border-radius:50%;
      background:{ui.OK}; animation:rt-pulse 1.6s ease-out infinite; flex:none; }}
  .rt-pill b {{ color:{ui.OK}; font-weight:740; }}
  .rt-pill .sep {{ color:{ui.FAINT}; }}
  .rt-pill::after {{ content:""; position:absolute; top:0; bottom:0; width:40px;
      background:linear-gradient(90deg,transparent,{ui.OK}22,transparent);
      animation:rt-sheen 3.2s ease-in-out infinite; }}
</style>
<div class="rt-pill"><span class="dot"></span>
  <b>LIVE</b><span class="sep">·</span>auto-refresh {_secs()}s
  <span class="sep">·</span>synced {ui.esc(synced)}</div>
""",
        unsafe_allow_html=True)


def engine() -> None:
    """Register the ticking fragment. Call once per run, after the controls.

    While Live is off this is a no-op. While on, a fragment re-runs itself
    every `interval` seconds; each real tick clears the data cache, stamps
    the sync time and forces a full-app rerun so every page repaints. The
    fragment body also runs once synchronously at registration — a time
    guard skips that first pass so we never tight-loop.
    """
    if not SS.get("rt_live"):
        SS["rt_last_tick"] = None
        return

    if SS.get("rt_last_tick") is None:
        SS["rt_last_tick"] = time.time()

    interval = _secs()

    @st.fragment(run_every=interval)
    def _tick():
        now = time.time()
        if now - (SS.get("rt_last_tick") or now) >= interval - 0.5:
            SS["rt_last_tick"] = now
            gsheets.refresh()
            SS["rt_last_sync"] = datetime.now().strftime("%H:%M:%S")
            st.rerun(scope="app")

    _tick()
