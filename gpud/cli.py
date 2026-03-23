#!/usr/bin/env python3
"""
gpud — serverless GPU scaling CLI

Usage:
  gpud daemon start          Start the background daemon
  gpud daemon stop           Stop the daemon
  gpud daemon status         Show daemon status

  gpud deploy  --config <yaml> [--name <name>]   Deploy a service
  gpud redeploy --config <yaml> [--name <name>]  Redeploy (rolling)
  gpud delete  <name>                            Delete a deployment
  gpud list                                      List all deployments
  gpud status  <name>                            Deployment detail
  gpud logs    <name>                            Recent events
  gpud init    [--name <name>]                   Write a starter YAML

  gpud watch   <name>                            Live-watch a deployment
"""

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

# Make sure package is importable when run directly
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from gpud.config import DeploymentConfig, EXAMPLE_YAML
from gpud.client import DaemonClient
from gpud.daemon import is_daemon_running, get_daemon_pid, DAEMON_STATE_FILE
from gpud.display import (
    print_deployments, print_deployment_detail, print_events,
    print_ok, print_error, print_info, print_header, c, _row,
    GREEN, RED, CYAN, GREY, B, R, YELLOW, WHITE,
)

CLIENT = DaemonClient()

# ── Helpers ────────────────────────────────────────────────────────────────────

def require_daemon():
    if not is_daemon_running():
        print_error("daemon is not running — start it with:  gpud daemon start")
        sys.exit(1)
    if not CLIENT.ping():
        print_error("daemon is running but not responding (try: gpud daemon stop && gpud daemon start)")
        sys.exit(1)

def load_config(config_path: str, name_override: str | None = None) -> tuple[str, DeploymentConfig]:
    cfg  = DeploymentConfig.from_yaml(config_path)
    name = name_override or cfg.name or pathlib.Path(config_path).stem.replace("deployment", "").strip("-_") or "app"
    cfg.name = name
    return name, cfg


# ── Subcommand handlers ────────────────────────────────────────────────────────

def cmd_daemon_start(args):
    if is_daemon_running():
        pid = get_daemon_pid()
        print_info(f"daemon already running (PID {pid})")
        return

    print_info("starting gpud daemon …")
    # Launch daemon as detached subprocess
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "gpud._daemon_main"],
        cwd=str(pathlib.Path(__file__).parent.parent),
        env=env,
        stdout=open(pathlib.Path.home() / ".gpud" / "daemon.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Wait for it to be ready
    if CLIENT.wait_ready(retries=40, delay=0.25):
        pid = get_daemon_pid()
        print_ok(f"daemon started  (PID {pid})")
        print_info(f"log: {pathlib.Path.home() / '.gpud' / 'daemon.log'}")
    else:
        print_error("daemon did not become ready — check ~/.gpud/daemon.log")
        sys.exit(1)


def cmd_daemon_stop(args):
    if not is_daemon_running():
        print_info("daemon is not running")
        return
    require_daemon()
    CLIENT.stop_daemon()
    for _ in range(20):
        if not is_daemon_running():
            print_ok("daemon stopped")
            return
        time.sleep(0.3)
    print_error("daemon did not stop cleanly")


def cmd_daemon_status(args):
    if not is_daemon_running():
        print_info(f"daemon is {c('not running', RED)}")
        return
    pid  = get_daemon_pid()
    live = CLIENT.ping()
    state_info = ""
    if DAEMON_STATE_FILE.exists():
        try:
            st = json.loads(DAEMON_STATE_FILE.read_text())
            n  = len(st.get("deployments", {}))
            state_info = f"  ({n} deployment(s))"
        except Exception:
            pass
    status = c("running", GREEN, B) if live else c("unresponsive", RED)
    print_info(f"daemon  {status}  PID={pid}{state_info}")


def cmd_deploy(args):
    require_daemon()
    try:
        name, cfg = load_config(args.config, args.name)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        sys.exit(1)

    print_info(f"deploying [{name}]  gpu={cfg.gpu_type}×{cfg.gpus}  min={cfg.min_scale} max={cfg.max_scale} …")
    resp = CLIENT.deploy(name, cfg.to_dict())

    if "error" in resp:
        print_error(resp["error"])
        sys.exit(1)
    print_ok(resp.get("message", "deployed"))
    print_info(f"run  gpud watch {name}  to monitor")


def cmd_redeploy(args):
    require_daemon()
    try:
        name, cfg = load_config(args.config, args.name)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        sys.exit(1)

    print_info(f"redeploying [{name}] …")
    resp = CLIENT.redeploy(name, cfg.to_dict())

    if "error" in resp:
        print_error(resp["error"])
        sys.exit(1)
    print_ok(resp.get("message", "redeployed"))


def cmd_delete(args):
    require_daemon()
    resp = CLIENT.delete(args.name)
    if "error" in resp:
        print_error(resp["error"])
        sys.exit(1)
    print_ok(resp.get("message", f"deleted {args.name}"))


def cmd_list(args):
    require_daemon()
    deps = CLIENT.list_deployments()
    print_header("deployments")
    print_deployments(deps)


def cmd_status(args):
    require_daemon()
    dep = CLIENT.get_deployment(args.name)
    if dep is None:
        print_error(f"deployment '{args.name}' not found")
        sys.exit(1)
    print_deployment_detail(dep)


def cmd_logs(args):
    require_daemon()
    events = CLIENT.get_events(limit=50)
    if args.name:
        events = [e for e in events if e.get("name") == args.name or e.get("kind") == "daemon_started"]
    print_header(f"events  {args.name or '(all)'}")
    print_events(events)


def cmd_init(args):
    name    = args.name or "deployment"
    outfile = pathlib.Path(f"{name}.yaml")
    if outfile.exists() and not args.force:
        print_error(f"{outfile} already exists (use --force to overwrite)")
        sys.exit(1)
    outfile.write_text(EXAMPLE_YAML)
    print_ok(f"wrote {outfile}")
    print_info("edit the file then run:  gpud deploy --config " + str(outfile))


def cmd_gpus(args):
    """Show real-time GPU metrics from NVML."""
    require_daemon()
    resp = CLIENT.send({"cmd": "gpus"})
    if "error" in resp:
        print_error(resp["error"])
        sys.exit(1)
    gpus = resp.get("gpus", [])
    if not gpus:
        print_info("no GPUs detected")
        return
    print_header(f"GPUs  ({len(gpus)} device(s))")
    hdrs   = ["IDX", "NAME", "VRAM USED", "VRAM TOTAL", "GPU%", "MEM%", "TEMP", "POWER"]
    widths = [4, 28, 12, 12, 7, 7, 8, 10]
    print(c(_row(*hdrs, widths=widths), GREY, B))
    for g in gpus:
        row = [
            str(g.get("index", "?")),
            g.get("name", "?"),
            f"{g.get('used_mem_gb', 0):.1f} GB",
            f"{g.get('total_mem_gb', 0):.1f} GB",
            f"{g.get('util_pct', 0):.0f}%",
            f"{g.get('mem_util_pct', 0):.0f}%",
            f"{g.get('temp_c', 0):.0f}°C",
            f"{g.get('power_w', 0):.0f}W",
        ]
        # Color GPU utilisation
        util = g.get("util_pct", 0)
        util_color = GREEN if util < 50 else YELLOW if util < 85 else RED
        row[4] = c(row[4], util_color)
        print(_row(*row, widths=widths))
    print()


def cmd_watch(args):
    """Live-watch a deployment, refreshing every 2 seconds."""
    require_daemon()
    name = args.name

    try:
        import curses
        _watch_curses(name, args.interval)
    except Exception:
        _watch_simple(name, args.interval)


def _watch_simple(name: str, interval: float):
    """Fallback watch — prints refreshes to stdout."""
    print_info(f"watching [{name}]  (Ctrl-C to quit)")
    try:
        while True:
            dep = CLIENT.get_deployment(name)
            if dep is None:
                print_error(f"deployment '{name}' not found")
                return
            os.system("clear")
            _print_banner()
            print_deployment_detail(dep)
            time.sleep(interval)
    except KeyboardInterrupt:
        print_info("stopped")


def _print_banner():
    print(c("   ██████╗ ██████╗ ██╗   ██╗██████╗ ", CYAN))
    print(c("  ██╔════╝ ██╔══██╗██║   ██║██╔══██╗", CYAN))
    print(c("  ██║  ███╗██████╔╝██║   ██║██║  ██║", CYAN))
    print(c("  ██║   ██║██╔═══╝ ██║   ██║██║  ██║", CYAN))
    print(c("  ╚██████╔╝██║     ╚██████╔╝██████╔╝", CYAN))
    print(c("   ╚═════╝ ╚═╝      ╚═════╝ ╚═════╝ ", CYAN))
    print(c("   GPU daemon", GREY))
    print()

def _watch_curses(name: str, interval: float):
    import curses

    def _draw(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN,   curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_CYAN,    curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_RED,     curses.COLOR_BLACK)

        while True:
            dep = CLIENT.get_deployment(name)
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            row  = 0

            def put(r, col, text, attr=0):
                try:
                    stdscr.addstr(r, col, text[:w-col-1], attr)
                except curses.error:
                    pass

            put(row, 2, "gpud", curses.color_pair(3) | curses.A_BOLD); row += 1
            put(row, 2, "serverless GPU scaling daemon", curses.color_pair(3)); row += 2

            if dep is None:
                put(row, 2, f"deployment '{name}' not found", curses.color_pair(4))
                stdscr.refresh()
                time.sleep(interval)
                continue

            rep    = dep.get("replicas", {})
            status = dep.get("status", "?")
            sattr  = curses.color_pair(1) if status == "running" else curses.color_pair(2)

            put(row, 2, f"deployment:  {name}", curses.A_BOLD); row += 1
            put(row, 2, f"status:      {status}", sattr); row += 1
            put(row, 2, f"gpu_type:    {dep.get('gpu_type','?')}  ×{dep.get('gpus_per_replica',1)}")
            row += 1
            put(row, 2, f"replicas:    {rep.get('ready',0)} ready / {rep.get('active',0)} active / {rep.get('desired',0)} desired")
            row += 1
            put(row, 2, f"rps:         {dep.get('rps',0):.1f}")
            row += 1
            ep = dep.get("endpoint") or "not yet ready"
            put(row, 2, f"endpoint:    {ep}", curses.color_pair(3)); row += 2

            put(row, 2, "workers", curses.A_BOLD | curses.A_UNDERLINE); row += 1
            put(row, 2, f"{'ID':<24} {'STATE':<14} {'GPU%':<8} {'VRAM GB':<10} {'REQS'}", curses.A_DIM)
            row += 1
            for w in dep.get("workers", []):
                wstate = w.get("state", "?")
                wattr  = curses.color_pair(1) if wstate == "ready" else (
                         curses.color_pair(2) if wstate == "busy" else
                         curses.color_pair(3))
                util   = f"{w.get('gpu_util_pct',0):.0f}%" if wstate in ("ready","busy") else "—"
                vram   = f"{w.get('vram_used_gb',0):.1f}" if wstate in ("ready","busy") else "—"
                put(row, 2,
                    f"{w['worker_id']:<24} {wstate:<14} {util:<8} {vram:<10} {w.get('requests_served',0)}",
                    wattr)
                row += 1
                if row >= h - 2:
                    break

            put(h-1, 2, "  q/Ctrl-C to quit    refreshing every {:.0f}s".format(interval), curses.A_DIM)
            stdscr.refresh()

            # Wait, checking for quit key
            deadline = time.time() + interval
            while time.time() < deadline:
                key = stdscr.getch()
                if key in (ord("q"), ord("Q"), 3):  # q or Ctrl-C
                    return
                time.sleep(0.1)

    try:
        import curses as _c
        _c.wrapper(_draw)
    except KeyboardInterrupt:
        pass
    print_info("stopped")


# ── Parser ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpud",
        description="gpud — serverless GPU scaling daemon & CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version="gpud 0.1.0")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # daemon
    pd = sub.add_parser("daemon", help="daemon lifecycle")
    dss = pd.add_subparsers(dest="daemon_cmd", metavar="<subcommand>")
    dss.add_parser("start",  help="start the daemon")
    dss.add_parser("stop",   help="stop the daemon")
    dss.add_parser("status", help="daemon status")

    # deploy
    pp = sub.add_parser("deploy",   help="deploy a service")
    pp.add_argument("--config", "-c", required=True, metavar="FILE", help="path to deployment.yaml")
    pp.add_argument("--name",   "-n", metavar="NAME", help="override deployment name")

    # redeploy
    pr = sub.add_parser("redeploy", help="rolling redeploy")
    pr.add_argument("--config", "-c", required=True, metavar="FILE")
    pr.add_argument("--name",   "-n", metavar="NAME")

    # delete
    pd2 = sub.add_parser("delete",  help="delete a deployment")
    pd2.add_argument("name", metavar="NAME")

    # list
    sub.add_parser("list",   help="list deployments")

    # status
    ps = sub.add_parser("status",  help="deployment status")
    ps.add_argument("name", metavar="NAME")

    # logs / events
    pl = sub.add_parser("logs",    help="view event log")
    pl.add_argument("name", metavar="NAME", nargs="?", default=None)

    # init
    pi = sub.add_parser("init",    help="write a starter deployment.yaml")
    pi.add_argument("--name",  "-n", default="deployment", metavar="NAME")
    pi.add_argument("--force", "-f", action="store_true", help="overwrite existing file")

    # gpus
    sub.add_parser("gpus",   help="show real-time GPU metrics")

    # watch
    pw = sub.add_parser("watch",   help="live-watch a deployment")
    pw.add_argument("name", metavar="NAME")
    pw.add_argument("--interval", "-i", type=float, default=2.0, metavar="SECS")

    return p


def main():
    p      = build_parser()
    args   = p.parse_args()

    if args.command is None:
        _print_banner()
        p.print_help()
        return

    pathlib.Path.home().joinpath(".gpud").mkdir(parents=True, exist_ok=True)

    dispatch = {
        "daemon":   _daemon_dispatch,
        "deploy":   cmd_deploy,
        "redeploy": cmd_redeploy,
        "delete":   cmd_delete,
        "list":     cmd_list,
        "status":   cmd_status,
        "logs":     cmd_logs,
        "init":     cmd_init,
        "gpus":     cmd_gpus,
        "watch":    cmd_watch,
    }
    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        p.print_help()


def _daemon_dispatch(args):
    dc = {
        "start":  cmd_daemon_start,
        "stop":   cmd_daemon_stop,
        "status": cmd_daemon_status,
    }
    if args.daemon_cmd is None:
        print_error("specify: daemon start | stop | status")
        sys.exit(1)
    dc[args.daemon_cmd](args)


if __name__ == "__main__":
    main()