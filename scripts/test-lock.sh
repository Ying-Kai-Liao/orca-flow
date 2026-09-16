#!/usr/bin/env bash
# Test lock: on this machine, let at most N worktrees run tests at the same time (default 1).
#
# Why: a test suite that boots a database and replays every migration is expensive. Six
# worktrees running the full suite at once on a 16 GB laptop got one run killed by the OOM
# killer and made a pre-deploy check time out. Serialising costs minutes; an OOM costs the batch.
#
# Why flock and not a mkdir lock: a mkdir lock has to decide for itself whether the holder
# died, and two waiters deciding at the same moment will delete the slot one of them just
# took — which is exactly what happens after an OOM, when everyone is queued. flock is
# kernel-managed: the lock is released when the last process holding that file descriptor
# exits, so there is nothing to decide. fd 9 is inherited by the test command, so the lock
# outlives this wrapper being killed while the tests are still running.
#
# The lock files live in <git-common-dir>/orca-flow/test-slots: every worktree sees the same
# ones, and they are outside the working tree, so git status stays clean.
#
# Usage:
#   test-lock.sh npx vitest run src/foo.test.ts --maxWorkers=1
#   test-lock.sh npm test          # the merge queue queues for the full suite too
#   test-lock.sh --status
# Slots: $ORCA_FLOW_TEST_SLOTS, else worker.test_slots from the orca-flow config, else 1.
# Queueing plus the run often exceeds the Bash tool's 2-minute default: use run_in_background
# or raise the timeout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SLOTS="${ORCA_FLOW_TEST_SLOTS:-}"
if [ -z "$SLOTS" ] && [ -f "$HERE/config.py" ]; then
  SLOTS="$(python3 "$HERE/config.py" get worker.test_slots 2>/dev/null || true)"
fi
case "$SLOTS" in ''|*[!0-9]*) SLOTS=1 ;; esac

COMMON="$(git rev-parse --path-format=absolute --git-common-dir)"
DIR="$COMMON/orca-flow/test-slots"
mkdir -p "$DIR"
WHO="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"

# Non-blocking flock on fd 9. macOS has no flock(1), so borrow python's fcntl: the lock
# belongs to the open file description behind the fd, so it survives python exiting and
# stays held by this shell's fd 9.
flock_fd9() {
  python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null
}

show() {
  local i f found=0
  for i in $(seq 1 "$SLOTS"); do
    f="$DIR/slot-$i.lock"
    [ -e "$f" ] || continue
    # Try the lock in a subshell: getting it means nobody holds it (released on exit).
    if ( exec 9>>"$f"; flock_fd9 ); then continue; fi
    found=1
    echo "  slot-$i: $(cat "$DIR/slot-$i.owner" 2>/dev/null || echo '?')"
  done
  if [ "$found" = 0 ]; then echo "  (nothing running)"; fi
}

if [ "${1:-}" = --status ]; then
  echo "test lock, ${SLOTS} slot(s):"
  show
  exit 0
fi
if [ $# -eq 0 ]; then
  echo "usage: test-lock.sh <command...> | --status" >&2
  exit 64
fi

SLOT=""
try_acquire() {
  local i
  for i in $(seq 1 "$SLOTS"); do
    exec 9>>"$DIR/slot-$i.lock"
    if flock_fd9; then
      SLOT="slot-$i"
      # The owner file is only there for --status; the lock never reads it, so a stale
      # one can't cause a wrong decision.
      echo "$$ $WHO $(date '+%H:%M:%S') $*" > "$DIR/slot-$i.owner"
      return 0
    fi
    exec 9>&-
  done
  return 1
}

waited=0
until try_acquire "$@"; do
  if [ $((waited % 60)) -eq 0 ]; then
    echo "[test-lock] waiting for a slot (${SLOTS} total), ${waited}s so far. Currently:"
    show
  fi
  sleep 5
  waited=$((waited + 5))
done
if [ "$waited" -gt 0 ]; then echo "[test-lock] got $SLOT after ${waited}s."; fi

set +e
"$@"
code=$?
set -e
exit "$code"
