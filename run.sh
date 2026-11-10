#!/usr/bin/env bash
# One-command version of RUNBOOK.md: starts MySQL, Redis, the two Celery workers
# (default + browser), Celery beat, and Django. Ctrl+C stops everything (Django runs in the
# foreground; the rest are background jobs killed on exit).
#
# Redis/Celery output goes to logs/ since they're backgrounded — tail those
# if something looks wrong (e.g. `tail -f logs/celery_worker.log`).

set -e
cd "$(dirname "$0")"
source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

PIDS=()
cleanup() {
    echo ""
    echo "Stopping background services..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
# EXIT alone is enough — it also fires when INT/TERM end the script, so
# trapping all three would run cleanup twice
trap cleanup EXIT

# 1. MySQL — safe to call even if it's already running
echo "Starting MySQL (may prompt for your sudo password)..."
sudo service mysql start

# 2. Redis
if redis-cli ping >/dev/null 2>&1; then
    echo "Redis already running."
else
    echo "Starting Redis..."
    redis-server --daemonize no > "$LOG_DIR/redis.log" 2>&1 &
    PIDS+=($!)
    sleep 1
fi

# 3. Celery workers: the default queue, and a separate single-job worker for
# everything that drives a browser so two Chrome sessions never overlap
echo "Starting Celery worker (default queue)..."
celery -A TGPFflows worker -Q celery --loglevel=info > "$LOG_DIR/celery_worker.log" 2>&1 &
PIDS+=($!)

echo "Starting Celery browser worker (one job at a time)..."
celery -A TGPFflows worker -Q browser -c 1 -n browser@%h --loglevel=info > "$LOG_DIR/celery_browser.log" 2>&1 &
PIDS+=($!)

# 4. Celery beat
echo "Starting Celery beat..."
celery -A TGPFflows beat --loglevel=info > "$LOG_DIR/celery_beat.log" 2>&1 &
PIDS+=($!)

sleep 1
echo ""
echo "Redis/Celery logs are in $LOG_DIR/"
echo "Starting Django — Ctrl+C stops everything."
echo ""

# 5. Django — foreground, so Ctrl+C here triggers cleanup for the rest
# --insecure: with DEBUG off, runserver still serves static files
python manage.py runserver --insecure
