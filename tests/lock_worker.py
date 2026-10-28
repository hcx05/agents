"""Test helper (run as a separate OS process): wait for the go flag, try to take a session lock, report WON/LOST, and
if it won hold the lock until the done flag appears (or hold_sec passes)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.sessionlock import SessionLock, SessionLocked  # noqa: E402

session_dir, go, done, hold = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
while not go.exists():
    pass                                                    # spin: all workers leave the starting line together
lock = SessionLock(session_dir)
try:
    lock.acquire()
except SessionLocked:
    print("LOST", flush=True)
    sys.exit(0)
print("WON", flush=True)
end = time.time() + hold
while not done.exists() and time.time() < end:
    time.sleep(0.01)
lock.release()
