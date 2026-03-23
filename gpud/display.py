"""
Terminal display helpers — no external dependencies.
"""

import json
import sys
import shutil
from datetime import datetime, timezone

# ANSI
R  = "\033[0m"
B  = "\033[1m"
D  = "\033[2m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
GREY   = "\033[90m"
MAGENTA = "\033[95m"

COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def c(text, *codes):
    if not COLOR:
        return text
    return "".join(codes) + str(text) + R


def status_color(s: str) -> str:
    s = s.lower()
    if s in ("running",):    return c(s, GREEN, B)
    if s in ("deploying", "scaling"): return c(s, YELLOW)
    if s in ("idle",):       return c(s, BLUE)
    if s in ("stopped",):    return c(s, GREY)
    if s in ("error",):      return c(s, RED, B)
    return s


def worker_state_color(s: str) -> str:
    if s == "ready":        return c(s, GREEN)
    if s == "busy":         return c(s, YELLOW)
    if s in ("pending", "initializing"): return c(s, CYAN)
    if s in ("draining", "stopped"):     return c(s, GREY)
    return s


def _term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _row(*cols, widths):
    parts = []
    for i, (col, w) in enumerate(zip(cols, widths)):
        s = str(col)
        # Strip ANSI for length calc
        raw = _strip_ansi(s)
        pad = max(0, w - len(raw))
        parts.append(s + " " * pad)
    return "  ".join(parts)


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _divider(char="─", width=None):
    w = width or min(_term_width(), 100)
    print(c(char * w, GREY))


def print_header(text: str):
    _divider()
    print(c(f"  {text}", CYAN, B))
    _divider()


def print_deployments(deps: list):
    if not deps:
        print(c("  no deployments", GREY))
        return

    headers = ["NAME", "STATUS", "READY", "GPU", "RPS", "ENDPOINT"]
    widths  = [22, 12, 10, 14, 8, 35]

    print(c(_row(*headers, widths=widths), GREY, B))
    _divider("─", sum(widths) + len(widths) * 2)

    for d in deps:
        rep = d.get("replicas", {})
        row = [
            c(d["name"], WHITE, B),
            status_color(d.get("status", "?")),
            f"{rep.get('ready', 0)}/{rep.get('active', 0)}",
            d.get("gpu_type", "-"),
            f"{d.get('rps', 0):.0f}",
            c(d.get("endpoint") or "—", GREY),
        ]
        print(_row(*row, widths=widths))


def print_deployment_detail(d: dict):
    rep  = d.get("replicas", {})
    name = d["name"]

    print_header(f"deployment  {name}")
    kv = [
        ("status",    status_color(d.get("status", "?"))),
        ("gpu_type",  d.get("gpu_type", "?")),
        ("gpus/rep",  d.get("gpus_per_replica", 1)),
        ("replicas",  f"{rep.get('ready',0)} ready / {rep.get('active',0)} active / {rep.get('desired',0)} desired"),
        ("rps",       f"{d.get('rps',0):.1f}"),
        ("endpoint",  c(d.get("endpoint") or "not yet ready", CYAN if d.get("endpoint") else GREY)),
        ("created",   _relative_time(d.get("created_at"))),
    ]
    for k, v in kv:
        print(f"  {c(k+':',GREY):<28} {v}")

    workers = d.get("workers", [])
    if workers:
        print()
        print(c("  workers", B))
        w_hdrs  = ["ID", "STATE", "GPU UTIL", "VRAM (GB)", "REQS", "READY"]
        w_widths = [22, 14, 10, 12, 7, 12]
        print("  " + c(_row(*w_hdrs, widths=w_widths), GREY, B))
        for w in workers:
            row = [
                c(w["worker_id"], WHITE),
                worker_state_color(w["state"]),
                f"{w.get('gpu_util_pct',0):.0f}%" if w["state"] in ("ready","busy") else "—",
                f"{w.get('vram_used_gb',0):.1f}" if w["state"] in ("ready","busy") else "—",
                str(w.get("requests_served", 0)),
                _relative_time(w.get("ready_at")) if w.get("ready_at") else c("pending", GREY),
            ]
            print("  " + _row(*row, widths=w_widths))
    _divider()


def print_events(events: list):
    if not events:
        print(c("  no events", GREY))
        return
    for ev in reversed(events[-20:]):
        ts   = _relative_time(ev.get("ts"))
        kind = c(ev.get("kind", "?"), CYAN)
        name = ev.get("name", "")
        extra = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("ts","kind","name"))
        print(f"  {c(ts,GREY):<20} {kind:<25} {c(name,WHITE):<25} {c(extra,GREY)}")


def print_ok(msg: str):
    print(c(f"  ✓  {msg}", GREEN, B))


def print_error(msg: str):
    print(c(f"  ✗  {msg}", RED, B), file=sys.stderr)


def print_info(msg: str):
    print(c(f"  ·  {msg}", CYAN))


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt  = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        s   = int((now - dt).total_seconds())
        if s < 5:   return "just now"
        if s < 60:  return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        return f"{s//3600}h ago"
    except Exception:
        return iso
