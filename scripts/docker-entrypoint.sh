#!/usr/bin/env sh
set -e

if [ -f /app/finalsay_seed.db.gz ]; then
    if [ ! -f /app/finalsay.db ] || [ "$(wc -c < /app/finalsay.db 2>/dev/null || echo 0)" -lt 100000 ]; then
        echo "==> Restoring pre-scraped notice corpus (3,279 notices)..."
        python -c "import gzip, shutil; shutil.copyfileobj(gzip.open('/app/finalsay_seed.db.gz', 'rb'), open('/app/finalsay.db', 'wb'))"
        echo "==> Restored finalsay.db successfully."
    fi
fi

echo "==> Configuring selected official notice sources..."
python -m finalsay.services.source_onboarding --apply

PORT="${PORT:-8000}"
echo "==> Starting FinalSay backend on port ${PORT}..."
exec python -m uvicorn finalsay.main:app --host 0.0.0.0 --port "${PORT}"
