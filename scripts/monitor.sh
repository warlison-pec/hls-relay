#!/usr/bin/env bash
# Compact live view of /health (reads it inside the container: works whatever
# host port/IP compose publishes). Usage: bash scripts/monitor.sh [canal 1-4] [interval_s]
set -euo pipefail
CONTAINER="${CONTAINER:-boxserver-hls-relay}"
CH="${1:-1}"
INTERVAL="${2:-5}"
while true; do
  docker exec -i -e CH="$CH" "$CONTAINER" python - <<'PY' || echo "relay sem resposta"
import json, os, time, urllib.request
port = os.environ.get("PORT", "8099")
ch = os.environ.get("CH", "1")
h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/ch/{ch}/health", timeout=5))
if h.get("status") == "off":
    print(time.strftime("%H:%M:%S"), f"canal {ch} desligado"); raise SystemExit
s, i, b, c = h["source"], h["ingest"], h["buffer"], h["clients"]
print(time.strftime("%H:%M:%S"), f"[{h['status']}]",
      f"ingest={i['ingest_ratio_60s']}x rt={i['avg_realtime_factor']}x",
      f"dl={i['avg_download_mbps']}Mbps stream={i['source_bitrate_mbps']}Mbps",
      f"seg_t={i['avg_segment_download_seconds']}s p95={i['p95_segment_download_seconds']}s",
      f"| pub={b['published_duration_seconds']}s cushion={b['cushion_duration_seconds']}s",
      f"underruns={b['underruns']}" + (f" REBUILD({b.get('delay_rebuilding')})" if b.get("delay_rebuilding") else ""),
      f"| ok={i['success_rate']} retries={i['retries']} incompl={i['incomplete_downloads']}",
      f"gaps={i['gaps']} disc={i['discontinuities']} resets={s['resets']}",
      f"| plErr={sum(s['playlist_errors'].values())} segErr={i['errors_by_category']}",
      f"| last_seg={i['seconds_since_last_segment']}s clients={c['active']}")
PY
  sleep "$INTERVAL"
done
