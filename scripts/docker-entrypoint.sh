#!/usr/bin/env sh
set -e

if [ -f /app/finalsay_seed.db.gz ]; then
    SHOULD_RESTORE=0
    if [ ! -f /app/finalsay.db ]; then
        SHOULD_RESTORE=1
    else
        python -c "import sqlite3; c=sqlite3.connect('/app/finalsay.db'); cur=c.cursor(); cur.execute(\"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notice'\"); exists=cur.fetchone()[0]; count=c.execute('SELECT count(*) FROM notice').fetchone()[0] if exists else 0; exit(0 if count < 3000 else 1)" 2>/dev/null && SHOULD_RESTORE=1 || true
    fi

    if [ "$SHOULD_RESTORE" -eq 1 ]; then
        echo "==> Restoring full pre-scraped notice corpus (3,279 notices)..."
        python -c "import gzip, shutil; shutil.copyfileobj(gzip.open('/app/finalsay_seed.db.gz', 'rb'), open('/app/finalsay.db', 'wb'))"
        echo "==> Restored finalsay.db successfully."
    fi
fi

echo "==> Configuring selected official notice sources..."
python -m finalsay.services.source_onboarding --apply

PORT="${PORT:-8000}"
echo "==> Starting FinalSay backend on port ${PORT}..."
exec python -m uvicorn finalsay.main:app --host 0.0.0.0 --port "${PORT}"
