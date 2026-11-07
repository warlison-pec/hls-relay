#!/usr/bin/env bash
# Onde cada cliente está em relação ao ao vivo da fonte. Uso: bash scripts/clients.sh [canal 1-4]
CONTAINER="${CONTAINER:-boxserver-hls-relay}"
docker exec -i -e CH="${1:-1}" -e TZ="${CLIENTS_TZ:-<-03>3}" "$CONTAINER" python - <<'PY'
import json, os, time, urllib.request
ch = os.environ.get("CH", "1")
h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('PORT','8099')}/ch/{ch}/health", timeout=5))
if h.get("status") == "off":
    print(f"Canal {ch} desligado."); raise SystemExit
b = h["buffer"]
seg = (b["published_duration_seconds"] / b["published_segments"]) if b["published_segments"] else 3.0
base = b["estimated_delay_behind_source_seconds"] or 0.0
now = time.time()
fmt = lambda s: f"{int(s//60)}min{int(s%60):02d}s"
print(f"Agora (BoxServer): {time.strftime('%H:%M:%S')}   status: {h['status']}")
print(f"Reserva no relay: {fmt(b['cushion_duration_seconds'])}   "
      f"borda publicada ~{fmt(base)} atrás da fonte   segmento ~{seg:.1f}s"
      + (f"   recompondo reserva ({b.get('delay_rebuilding')})" if b.get("delay_rebuilding") else ""))
clients = [c for c in h["clients"]["recent"] if c["idle_seconds"] <= 60]
if not clients:
    print("Nenhum cliente assistindo (último minuto).")
for c in clients:
    behind = c["segments_behind_live_edge"]
    if behind is None:
        print(f"- {c['ip']:<16} ainda não pediu segmentos"); continue
    total = base + behind * seg
    print(f"- {c['ip']:<16} {fmt(total)} atrás da fonte  "
          f"(vendo o ao vivo de ~{time.strftime('%H:%M:%S', time.localtime(now - total))})  "
          f"{behind} seg. atrás da borda  404={c['segment_404']}  {c['mb_sent']} MB  "
          f"ocioso {c['idle_seconds']}s")
PY
