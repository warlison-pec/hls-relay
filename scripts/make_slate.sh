#!/usr/bin/env bash
# Gera o vídeo de "aguarde" dos canais sob demanda (precisa de ffmpeg no host).
# Uso: bash scripts/make_slate.sh [-t titulo] [-s subtitulo] [-i imagem] [-o pasta] [-S 1920x1080] [-r 30000/1001]
# Resolução e quadros por segundo iguais aos dos canais (padrão 1080p a 29,97) evitam que o player
# precise trocar de formato na passagem para o canal real.
# A taxa é constante de propósito: proxies (Dispatcharr, Jellyfin) só entregam ao player depois de
# acumular dados, e um vídeo leve demais faria a mensagem demorar a aparecer.
# Com -i, a imagem substitui o fundo e o texto continua por cima (use -t "" -s "" para só a imagem).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
TITLE="Canal iniciando"; SUBTITLE="Aguarde o carregamento..."; IMAGE=""; OUT="slate"
SECONDS_TOTAL=120; SEGMENT=2; SIZE=1920x1080; FPS=30000/1001; BITRATE=1500   # kbit/s
while getopts "t:s:i:o:S:r:" opt; do
  case "$opt" in
    t) TITLE="$OPTARG" ;; s) SUBTITLE="$OPTARG" ;; i) IMAGE="$OPTARG" ;; o) OUT="$OPTARG" ;;
    S) SIZE="$OPTARG" ;; r) FPS="$OPTARG" ;;
    *) exit 2 ;;
  esac
done
[[ "$SIZE" =~ ^([0-9]+)x([0-9]+)$ ]] || { echo "Tamanho inválido: $SIZE (use LARGURAxALTURA)" >&2; exit 2; }
W=${BASH_REMATCH[1]}; H=${BASH_REMATCH[2]}
[[ "$FPS" =~ ^[0-9]+(/[0-9]+)?$ ]] || { echo "FPS inválido: $FPS" >&2; exit 2; }
GOP=$(awk "BEGIN{printf \"%d\", $SEGMENT * $FPS + 0.5}")   # one keyframe per segment
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
[ -f "$FONT" ] || { echo "Fonte não encontrada: $FONT" >&2; exit 1; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
printf '%s' "$TITLE" > "$WORK/title.txt"; printf '%s' "$SUBTITLE" > "$WORK/sub.txt"
FILTER="scale=$W:$H:force_original_aspect_ratio=decrease,pad=$W:$H:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
[ -n "$TITLE" ] && FILTER+=",drawtext=fontfile=$FONT:textfile=$WORK/title.txt:fontsize=$((H * 64 / 720)):fontcolor=white:x=(w-text_w)/2:y=(h/2)-$((H * 80 / 720))"
[ -n "$SUBTITLE" ] && FILTER+=",drawtext=fontfile=$FONT:textfile=$WORK/sub.txt:fontsize=$((H * 38 / 720)):fontcolor=0xb8c4d9:x=(w-text_w)/2:y=(h/2)+$((H * 20 / 720))"
if [ -n "$IMAGE" ]; then VIDEO=(-loop 1 -framerate "$FPS" -i "$IMAGE"); else VIDEO=(-f lavfi -i "color=c=0x10131a:s=${W}x${H}:r=$FPS"); fi
mkdir -p "$WORK/out"
ffmpeg -hide_banner -loglevel error -y "${VIDEO[@]}" -f lavfi -i anullsrc=r=48000:cl=stereo -t "$SECONDS_TOTAL" \
  -vf "$FILTER" -r "$FPS" -c:v libx264 -preset veryfast -tune stillimage -profile:v main \
  -x264-params nal-hrd=cbr:force-cfr=1 -b:v "${BITRATE}k" -minrate "${BITRATE}k" -maxrate "${BITRATE}k" -bufsize "$((BITRATE * 2))k" \
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
  -c:a aac -b:a 32k -f hls -hls_time "$SEGMENT" -hls_list_size 0 -hls_playlist_type vod \
  -hls_segment_filename "$WORK/out/%03d.ts" "$WORK/out/index.m3u8"
mkdir -p "$OUT"; rm -f "$OUT"/*.ts "$OUT"/index.m3u8; cp "$WORK"/out/* "$OUT"/; chmod -R a+rX "$OUT"
echo "Pronto: $OUT ($(ls "$OUT"/*.ts | wc -l) segmentos, $(du -sh "$OUT" | cut -f1))"
