#!/usr/bin/env bash
# Gera o vídeo de "aguarde" dos canais sob demanda (precisa de ffmpeg no host).
# Uso: bash scripts/make_slate.sh [-t titulo] [-s subtitulo] [-i imagem] [-o pasta]
# A taxa é constante de propósito: proxies (Dispatcharr, Jellyfin) só entregam ao player depois de
# acumular dados, e um vídeo leve demais faria a mensagem demorar a aparecer.
# Com -i, a imagem substitui o fundo e o texto continua por cima (use -t "" -s "" para só a imagem).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
TITLE="Canal iniciando"; SUBTITLE="Aguarde o carregamento..."; IMAGE=""; OUT="slate"
SECONDS_TOTAL=120; SEGMENT=2; FPS=25; BITRATE=1500   # kbit/s
while getopts "t:s:i:o:" opt; do
  case "$opt" in
    t) TITLE="$OPTARG" ;; s) SUBTITLE="$OPTARG" ;; i) IMAGE="$OPTARG" ;; o) OUT="$OPTARG" ;;
    *) exit 2 ;;
  esac
done
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
[ -f "$FONT" ] || { echo "Fonte não encontrada: $FONT" >&2; exit 1; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
printf '%s' "$TITLE" > "$WORK/title.txt"; printf '%s' "$SUBTITLE" > "$WORK/sub.txt"
FILTER="scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
[ -n "$TITLE" ] && FILTER+=",drawtext=fontfile=$FONT:textfile=$WORK/title.txt:fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h/2)-80"
[ -n "$SUBTITLE" ] && FILTER+=",drawtext=fontfile=$FONT:textfile=$WORK/sub.txt:fontsize=38:fontcolor=0xb8c4d9:x=(w-text_w)/2:y=(h/2)+20"
if [ -n "$IMAGE" ]; then VIDEO=(-loop 1 -framerate "$FPS" -i "$IMAGE"); else VIDEO=(-f lavfi -i "color=c=0x10131a:s=1280x720:r=$FPS"); fi
mkdir -p "$WORK/out"
ffmpeg -hide_banner -loglevel error -y "${VIDEO[@]}" -f lavfi -i anullsrc=r=48000:cl=stereo -t "$SECONDS_TOTAL" \
  -vf "$FILTER" -r "$FPS" -c:v libx264 -preset veryfast -tune stillimage -profile:v main \
  -x264-params nal-hrd=cbr:force-cfr=1 -b:v "${BITRATE}k" -minrate "${BITRATE}k" -maxrate "${BITRATE}k" -bufsize "$((BITRATE * 2))k" \
  -g $((SEGMENT * FPS)) -keyint_min $((SEGMENT * FPS)) -sc_threshold 0 \
  -c:a aac -b:a 32k -f hls -hls_time "$SEGMENT" -hls_list_size 0 -hls_playlist_type vod \
  -hls_segment_filename "$WORK/out/%03d.ts" "$WORK/out/index.m3u8"
mkdir -p "$OUT"; rm -f "$OUT"/*.ts "$OUT"/index.m3u8; cp "$WORK"/out/* "$OUT"/; chmod -R a+rX "$OUT"
echo "Pronto: $OUT ($(ls "$OUT"/*.ts | wc -l) segmentos, $(du -sh "$OUT" | cut -f1))"
