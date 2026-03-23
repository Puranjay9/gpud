"""
    Entry pointy for the daemoin subprocess
    involked by -> python -m gpud._daemon_main
"""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from gpud.daemon import GpudDaemon

if __name__ == "__main__":
    d= GpudDaemon()
    d.start()
