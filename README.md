# BoxServer HLS Relay v0.5.2

Relay HLS local para **uma** origem MPEG-TS **autorizada**. Recebe a transmissão
continuamente, valida cada segmento e publica uma playlist local estável em
`http://IP_DO_BOXSERVER:8099/live.m3u8`. Funciona com VLC, Dispatcharr, Fire TV/Smart TVs e
clientes HLS com MPEG-TS, inclusive via Tailscale. Não transcodifica (vídeo e áudio
originais, copiados byte a byte), não decifra DRM, não renova credenciais de terceiros, não
gera vídeo artificial e não esconde falhas reais da origem.

O diagnóstico completo da v0.3 (causas confirmadas × hipóteses, com funções e linhas) está em
**[DIAGNOSTICO.md](DIAGNOSTICO.md)**.

## Multicanal e painel web (v0.5)

O relay agora mantém **vários canais** (até 8 por padrão; `MAX_CHANNELS` no `.env` aceita até 32), cada um com o mesmo motor da v0.4: reserva/atraso,
recomposição, validação de segmentos e renovação de token. Os canais são independentes: se a
fonte de um cair, os outros seguem normalmente.

| Endereço | O que é |
|---|---|
| `http://IP:8099/admin` | **painel**: adicionar e remover canais; nome, URL, atraso, conexões e "Fixar servidor" de cada um; ligar, desligar e reiniciar |
| `http://IP:8099/live.m3u8` | canal 1 (o mesmo endereço da v0.4: o Dispatcharr não precisa mudar) |
| `http://IP:8099/ch/N/live.m3u8` | canal N (o número aparece no cartão do canal) |
| `http://IP:8099/ch/N/health` | métricas do canal N |
| `http://IP:8099/health` | métricas do canal 1 + resumo de todos |

**Como funciona:**
- Na **primeira** partida, o canal 1 é criado com o `HLS_SOURCE_URL` do `.env` e já começa
  ligado. A partir daí, os canais são gerenciados no painel e ficam salvos em `data/channels.json`.
  Trocar o `HLS_SOURCE_URL` no `.env` não muda mais o canal 1.
- Os ajustes gerais (atraso padrão, conexões padrão, `MASTER_REFRESH_SECONDS`, `MAX_CACHE_MB`
  etc.) continuam vindo do `.env`. No painel, cada canal pode usar o padrão ou um valor próprio.
- Salvar uma URL, atraso ou conexões diferentes, ou clicar em "Fixar servidor", **num canal ligado reinicia o canal**, e a
  reserva começa do zero (o painel pede confirmação). Mudar só o nome não reinicia nada.
- **Adicionar canal** cria um cartão novo (desligado). Preencha a URL, salve e ligue.
  **Remover** desliga o canal e apaga o cadastro. O número de um canal removido **nunca é
  reaproveitado**, para um link antigo salvo no Dispatcharr ou numa TV não cair em outro canal.
  Um endereço de canal removido responde `404`.
- **Modo de cada canal** (campo "Modo" no cartão):
  - **Sempre ligado:** liga e desliga pelo painel e volta ligado quando o container reinicia.
  - **Sob demanda:** fica **em espera**, sem conexão com a fonte e sem memória. O **primeiro
    pedido** da playlist (`/ch/N/live.m3u8`, ou `/live.m3u8` no canal 1) liga o canal sozinho.
    Enquanto a reserva enche, a resposta é `503 Buffering: Xs of Ys`; depois disso, o canal toca
    normalmente. Após **20 minutos sem nenhum pedido** de playlist ou segmento (ajustável por canal
    ou por `DEMAND_IDLE_MINUTES` no `.env`), ele desliga sozinho e volta para "Em espera".
    Bom para canais de reserva: quando a fonte principal cair, é só abrir o link da reserva.
  - O tempo até a imagem aparecer num canal sob demanda é o **atraso** escolhido para ele. Para
    reservas, um atraso menor (1–2 min) faz o canal subir mais rápido.
  - **Qualquer pedido acorda o canal**, inclusive testes automáticos de canais (Dispatcharr,
    STRM Indexer). Nesse caso ele roda até completar o tempo sem clientes e desliga.
  - No Dispatcharr, a primeira tentativa num canal em espera pode estourar o tempo de partida dele
    (Channel Initialization Timeout, 60 s por padrão), mas o pedido já ligou o canal: tente de
    novo depois que a reserva encher.
  - **Tela de "aguarde" (opcional, desligada por padrão):** gere o vídeo com
    `bash scripts/make_slate.sh -t "Canal iniciando" -s "Aguarde o carregamento..."` (ou `-i imagem.png`),
    monte a pasta `./slate` em `/slate:ro` no compose e defina `SLATE_DIR=/slate` no `.env`. Enquanto a
    reserva enche, o canal sob demanda toca esse vídeo em vez de responder `503`, e passa sozinho para o
    canal real. Se a fonte não entregar em `atraso + 120 s`, volta ao `503`. Canais "Sempre ligado" não
    mudam. O áudio do vídeo é AAC: em canais com áudio AC-3, clientes baseados em ffmpeg podem perder o
    som após a troca.
    O relay acerta o relógio (timestamps) do vídeo de "aguarde" pelo do canal real, para que a troca não
    congele a imagem em clientes que remuxam com ffmpeg (Dispatcharr), e começa o canal no segmento que
    continua esse relógio (log `event=slate_switch aligned=True`). O vídeo sai por padrão em 1080p a
    29,97 fps, como os canais; para outra fonte use `-S LARGURAxALTURA -r FPS`.
- Canal desligado não consome nada: nenhuma conexão com a fonte, nenhuma memória. A
  `/ch/N/live.m3u8` dele responde `503 Canal N desligado`.
- Os canais que estavam ligados voltam ligados quando o container reinicia.

**Senha do painel:** o instalador gera uma senha aleatória, mostra no final e guarda em
`data/admin_password`. Para usar outra, coloque `ADMIN_PASSWORD=...` no `.env`, que tem
prioridade, ou edite esse arquivo, e recrie o container. O painel só aceita acessos das redes
de `ALLOWED_CLIENT_CIDRS` (rede local, Tailscale), e cada tentativa de senha errada é registrada
no log (`event=admin_auth_failed`).

**O que o instalador acrescenta** sem tocar no `compose.yaml` nem no `.env`:
- `data/`, pasta gravável pelo usuário do container (65534), com `channels.json` e `admin_password`;
- `compose.override.yaml`, que o Docker Compose lê automaticamente junto com o `compose.yaml`:
  ```yaml
  services:
    hls-relay:
      volumes:
        - ./data:/data
      environment:
        RELAY_STATE_DIR: /data
      mem_limit: 2g
  ```
  Use `RELAY_MEM_LIMIT=3g sudo -E bash scripts/install_v04.sh ...` para outro limite. Se você já
  tiver um `compose.override.yaml`, ele não é alterado e o instalador mostra o que acrescentar.

**Memória:** cada canal ligado guarda a sua reserva. O topo do painel mostra a memória em uso
do container e a soma prevista de todos os canais, e fica vermelho acima de 85% do limite.
Referência para 5 min de atraso + 30 segmentos publicados:

| Canal | Memória aproximada |
|---|---|
| 2,8 Mbit/s (720p) | ~250 MB |
| 6 Mbit/s | ~500 MB |
| 11 Mbit/s (1080p) | ~650–900 MB |

Quatro canais de 2,8 Mbit/s cabem em 2 GB; para mais canais ligados ao mesmo tempo, aumente `mem_limit` (~300 MB por canal desse tipo). Com canais 1080p, reduza o atraso desses canais
ou aumente `mem_limit`. `MAX_CACHE_MB` continua sendo o teto **por canal**.

**Conexões com o provedor:** cada canal ligado usa `MAX_PARALLEL_DOWNLOADS` conexões (ou o
valor escolhido no painel), mais a da playlist. Se o provedor limitar conexões simultâneas por
conta, some todos os canais ligados.

**Monitorar um canal específico:**
```bash
bash scripts/monitor.sh 2        # canal 2
bash scripts/clients.sh 3        # clientes do canal 3
docker exec boxserver-hls-relay python /app/relay.py --probe 4 --ch 2   # vazão da fonte do canal 2
docker logs -f boxserver-hls-relay 2>&1 | grep 'ch=2'                    # eventos só do canal 2
```

## Atraso mínimo de 2 minutos (v0.4.1)

Por padrão o canal fica **pelo menos 120 s atrás da origem** (`LIVE_DELAY_SECONDS=120`).
O relay continua baixando tudo em tempo real, mas só anuncia na `/live.m3u8` o vídeo que já
tem 2 minutos de reserva atrás dele. A liberação é cadenciada, 1 segmento por duração.

- **Origem parada ou lenta por até ~2 min:** a playlist continua crescendo com a reserva e o
  player não percebe. O evento aparece no log (`event=stall`) e no `/health`
  (`cushion_duration_seconds` caindo).
- **A origem volta e entrega o atrasado** (rajada, ou segmentos ainda na janela dela): a
  reserva se refaz sozinha, até o teto de `LIVE_DELAY_MAX_SECONDS` (150 s).
- **Vídeo que a origem perdeu de vez** (saiu da janela antes de ser baixado) não volta.
  A reserva fica menor que 120 s até a próxima recuperação. O relay não inventa vídeo e não
  pausa o cliente para recompor.
- **Não salva:** se a vazão até o BoxServer for menor que o bitrate o tempo todo
  (`ingest_ratio_60s` < 1), a reserva só adia o congelamento por 2 minutos.
- **Partida:** depois de **reiniciar o container**, a `/live.m3u8` responde `503 Buffering: Xs of
  120s` por ~2 min (cerca de 129 s com segmentos de 2,9 s). Com o relay já rodando, abrir o
  canal é imediato, sempre 2 min atrás do ao vivo.
- **Memória:** 150 s de reserva + 30 segmentos publicados + 30 s de graça ≈ 270 s × 1,46 MB/s ≈
  **400 MB** a 11,7 Mbit/s. Cabe em `MAX_CACHE_MB=512` e `mem_limit: 768m`. O relay calcula isso
  com o bitrate real e avisa (`event=memory_estimate`) se não couber.

Para voltar ao comportamento anterior (colchão em segmentos), use `LIVE_DELAY_SECONDS=0`.

### Recompor a reserva depois de quedas da origem (v0.4.2)

Segundos perdidos pela fonte só voltam como **espera**. Para ficar de novo 5 min atrás do ao
vivo, alguém precisa esperar esses segundos. O relay faz isso de duas formas:

1. **Sem ninguém assistindo (automático, custo zero).** Se nenhum cliente pedir a playlist ou
   segmentos por `LIVE_DELAY_IDLE_SECONDS` (30 s), o relay segura a publicação até a reserva
   voltar ao alvo. Quando alguém abre o canal, a reserva já está cheia. Log:
   `event=delay_rebuild state=start/stop`. No monitor aparece `REBUILD(idle)`. Para ser
   reconhecido "sem ninguém", o Dispatcharr precisa encerrar o stream quando ninguém assiste.
2. **Com alguém assistindo (opcional, desligado).** `LIVE_DELAY_REBUILD_PAUSE_SECONDS=2`
   segura a publicação por 2 s no máximo a cada `LIVE_DELAY_REBUILD_EVERY_SECONDS` (300 s),
   só enquanto a reserva estiver mais de 10 s abaixo do alvo. Cada pausa pode aparecer como
   uma espera de ~2 s no player. Recompõe ~24 s por hora. Log: `event=delay_rebuild_pause`.

Com a reserva no alvo, nenhuma das duas age. A tolerância de 10 s evita que a variação
normal de um segmento dispare a recomposição.

## O que mudou da v0.3 para a v0.4

| Área | v0.4 |
|---|---|
| Arquitetura | Threads separados: **poller** (playlist), **workers** de download, **commit** ordenado, **liberação cadenciada** e **HTTP**. Download lento não segura a playlist nem os outros downloads. |
| Playlist da origem | Recarregada a cada `min(POLL_INTERVAL, TARGETDURATION/2)`. A master é resolvida uma vez e de novo só após erro. URLs relativas são resolvidas contra a URL **final** (depois de redirect). |
| Download | Até `MAX_PARALLEL_DOWNLOADS` (2) conexões keep-alive. `Content-Length` conferido. Prazo total por tentativa. Timeout de leitura automático (2 × TARGETDURATION). Retry com backoff exponencial e jitter. 404 não é repetido se o segmento já saiu da janela da origem. Com 429/503, cai sozinho para 1 conexão. |
| Integridade | Valida MPEG-TS (sync 0x47, múltiplo de 188 bytes, PAT/PMT). Segmento truncado ou HTML de erro nunca é publicado. Duplicatas são descartadas por hash do conteúdo. |
| Identidade | Sequência + **nome do arquivo** (ignora tokens na query, diretórios e troca de host do CDN). |
| Continuidade | Sequência local monotônica (inclusive após reiniciar o container). `EXT-X-DISCONTINUITY` decidido pelo **PTS real** do vídeo, pela tag da origem ou, sem PTS, por sequência. Detecta reinício da origem (sequência recua ou nome muda) e playlists atrasadas de CDN. |
| Buffer | `START_BEHIND_SEGMENTS` virou um **colchão cadenciado**: segmentos já baixados são liberados 1 por duração. Se a origem para, a playlist continua crescendo até o colchão esvaziar. `MAX_CUSHION_SEGMENTS` limita o atraso. Janela publicada protegida contra limpeza e graça por tempo. |
| Saída HLS | Borda ao vivo nunca recua. `TARGETDURATION` monotônico. DSN coerente. HTTP/1.1, `HEAD`. |
| Observabilidade | `/health` com vazão, fator de tempo real, taxa de ingestão, colchão, lacunas, erros por categoria, estado da origem e dos clientes. Logs estruturados `event=...` (ou JSON). `relay.py --probe`. |
| Segurança | Redes de clientes permitidas (`ALLOWED_CLIENT_CIDRS`). Proteção contra SSRF em redirects e URLs da playlist. Logs sem token, senha ou diretórios. Container não-root e somente leitura (já era). |

## Instalar (sem tocar `.env`, `compose.yaml`, portas ou outros containers)

Copie `boxserver-hls-relay-v0.4.zip` para o BoxServer (por exemplo, `/mnt/sdb1/`) e rode:

```bash
cd /mnt/sdb1
unzip -o boxserver-hls-relay-v0.4.zip
cd boxserver-hls-relay-v0.4

# 1) testes offline (opcional, ~2 min, usa o python3 do Ubuntu; não acessa a origem)
bash scripts/run_tests.sh --quick

# 2) instalação: backup completo, tag da imagem atual, cópia dos arquivos, rebuild só deste projeto
sudo bash scripts/install_v04.sh /mnt/sdb1/hls-relay
```

Se uma v0.4.x anterior já estiver instalada, o mesmo comando atualiza para a v0.5 (ver "Multicanal e painel web"). Ele faz um backup próprio (`hls-relay-v0.4.x-...`), e o backup da v0.3 continua sendo o alvo do rollback.

O instalador:

1. confere se o pacote está completo e se `relay.py` é válido;
2. cria `/mnt/sdb1/hls-relay-backups/hls-relay-v0.3-AAAAMMDD-HHMMSS.tar.gz` (diretório inteiro,
   `.env` incluído, permissão 600, com checksum), **fora** do contexto de build;
3. marca a imagem em uso como `boxserver-hls-relay:v0.3-AAAAMMDD-HHMMSS`;
4. copia apenas `relay.py`, `Dockerfile`, `.dockerignore`, `README.md`, `DIAGNOSTICO.md`, `tests/`,
   `scripts/` e `.env.example.v0.4`. **`.env` e `compose.yaml` não são tocados**;
5. roda `docker compose build` e `docker compose up -d --force-recreate` **dentro de
   `/mnt/sdb1/hls-relay`**, o que só afeta esse projeto compose;
6. espera o `/health` responder (via `docker exec`, independente da porta publicada).

Instalação manual equivalente:

```bash
cd /mnt/sdb1/hls-relay
STAMP=$(date +%Y%m%d-%H%M%S)
sudo mkdir -p ../hls-relay-backups
sudo tar -C .. -czf ../hls-relay-backups/hls-relay-v0.3-$STAMP.tar.gz hls-relay
docker image tag "$(docker inspect --format '{{.Image}}' boxserver-hls-relay)" boxserver-hls-relay:v0.3-$STAMP
cp /mnt/sdb1/boxserver-hls-relay-v0.4/{relay.py,Dockerfile,.dockerignore} .
docker compose build && docker compose up -d --force-recreate
```

Todas as chaves do `.env` da v0.3 continuam válidas, com o mesmo significado. As chaves novas
são opcionais e estão em `.env.example.v0.4`.

## Verificar e monitorar

```bash
# estado, versão e healthcheck do Docker
docker ps --filter name=boxserver-hls-relay

# painel compacto atualizado a cada 5 s
bash /mnt/sdb1/hls-relay/scripts/monitor.sh

# onde cada cliente está (quanto atrás da fonte, horário do ao vivo que está vendo)
bash /mnt/sdb1/hls-relay/scripts/clients.sh

# JSON completo (se a porta estiver publicada em 127.0.0.1)
curl -s http://127.0.0.1:8099/health | python3 -m json.tool

# playlist publicada
curl -s http://127.0.0.1:8099/live.m3u8 | grep -E '^#EXT-X-(MEDIA-SEQUENCE|DISCONTINUITY|TARGETDURATION)' | head

# só os eventos que importam para diagnóstico
docker logs -f boxserver-hls-relay 2>&1 | grep -E 'event=(state_change|stall|summary|gap|segment_retry|segment_failed|discontinuity|source_reset|cushion_empty|playlist_error|parallel_downloads_reduced|client_segment_expired)'

# medição de vazão: baixa os 4 segmentos mais novos em série e 2 em paralelo (não imprime URLs)
docker exec boxserver-hls-relay python /app/relay.py --probe 4

# teste de reprodução de 10 min sem tocar no Dispatcharr (precisa de ffmpeg no host)
ffmpeg -hide_banner -loglevel error -i http://127.0.0.1:8099/live.m3u8 -t 600 -map 0:v:0 -f null - 2>&1 \
  | grep -cE 'PPS|decode_slice_header|no frame'
```

Se a porta estiver publicada só no IP do Tailscale, troque `127.0.0.1` por esse IP ou use o
`monitor.sh`, que lê de dentro do container.

### Como ler o `/health`

| Campo | Saudável | O que indica quando ruim |
|---|---|---|
| `status` | `ok` | `stalled:playlist_failing` = a playlist da origem falha. `stalled:origin_not_advancing` = a playlist responde, mas não aparecem segmentos novos (problema na origem). `stalled:segment_downloads_failing` = há segmentos novos, mas o download falha. `degraded:ingest_slower_than_realtime` = a coleta não acompanha. |
| `ingest.ingest_ratio_60s` | ≈ 1,0 | < 0,95 de forma sustentada = **o player vai congelar**, com qualquer buffer |
| `ingest.avg_realtime_factor` | ≥ 1,5 | duração ÷ tempo de download por segmento. < 1,0 = uma conexão é mais lenta que o vídeo |
| `ingest.avg_download_mbps` × `source_bitrate_mbps` | dl ≫ bitrate | vazão real contra a necessária |
| `ingest.avg/p95_segment_download_seconds` | ≪ duração do segmento | tempo gasto por segmento |
| `ingest.incomplete_downloads`, `errors_by_category` | baixos | `incomplete` = conexão cortada no meio. `http_404` = origem. `timeout` = enlace. |
| `ingest.gaps`, `missing_segments` | 0 | segmentos que a origem não entregou ou que saíram da janela antes do download |
| `ingest.keyframe_start_ratio` | 1,0 | < 1 = a origem corta segmentos fora de IDR; erros `non-existing PPS` são esperados após lacunas e na entrada do canal |
| `buffer.cushion_duration_seconds` | 120–150 | vídeo baixado e ainda não anunciado (reserva contra pausas da origem) |
| `buffer.estimated_delay_behind_source_seconds` | ≈ 130–160 | atraso aproximado em relação ao ao vivo da origem |
| `buffer.buffering` / `buffering_progress_seconds` | `false` | `true` só nos ~2 min após reiniciar o container |
| `buffer.underruns` / `underrun_seconds` | 0 | vezes e segundos em que o colchão esvaziou e a publicação esperou a origem |
| `clients.recent[].segments_behind_live_edge` | pequeno e estável | crescendo = o cliente não acompanha (rede do cliente, Tailscale via DERP) |
| `clients.segment_404_total` | 0 | cliente pedindo segmento já expirado (cliente muito atrasado) |

## Ajustes

| Chave | Padrão | Quando mudar |
|---|---|---|
| `LIVE_DELAY_SECONDS` | **120** | Atraso mínimo em relação à origem. Maior = mais proteção, mais memória e partida mais lenta após reiniciar. |
| `LIVE_DELAY_MAX_SECONDS` | atraso + 30 | Teto do atraso. O excedente é liberado na hora. |
| `LIVE_DELAY_IDLE_SECONDS` | 30 | Sem clientes por esse tempo, o relay recompõe a reserva parada. |
| `LIVE_DELAY_REBUILD_PAUSE_SECONDS` / `..._EVERY_SECONDS` | 0 (desligado) / 300 | Pausas curtas para recompor a reserva com alguém assistindo. |
| `START_BEHIND_SEGMENTS` / `MAX_CUSHION_SEGMENTS` | 4 / 8 | Só valem com `LIVE_DELAY_SECONDS=0` (colchão contado em segmentos). |
| `MAX_PARALLEL_DOWNLOADS` | 2 | Use **1** se o provedor limitar conexões simultâneas (403/429 em `errors_by_category`). O relay também cai para 1 sozinho com 429/503. |
| `SEGMENT_READ_TIMEOUT` | auto (6 s para segmentos de 3 s) | Tempo sem bytes até abandonar a conexão e tentar outra. |
| `SEGMENT_GIVE_UP_SECONDS` | 30 | Depois disso o segmento é declarado lacuna e a fila segue. |
| `PUBLISH_WINDOW_SEGMENTS` / `WINDOW_SEGMENTS` | 30 / 40 | Janela publicada e retida. **Não aumentam a proteção contra travamento**: o player começa perto do fim. |
| `ALLOWED_CLIENT_CIDRS` | loopback, redes privadas, Tailscale | Se um cliente legítimo receber 403, o IP dele aparece em `event=client_denied`. |
| `SOURCE_ALLOWED_HOSTS` | vazio (qualquer host **público**) | Se a origem usa um CDN conhecido, restrinja: `.cdn.exemplo.com`. |
| `LOG_SEGMENTS` | true | Uma linha por segmento (~20/min). Depois do diagnóstico, use `false`; ficam o resumo por minuto e as anomalias. |
| `LOG_FORMAT` | text | `json` para ingestão em ferramentas de log. |

Memória: com `LIVE_DELAY_SECONDS=120`, `MAX_CACHE_MB=512` e 11,7 Mbit/s, o uso fica em ~400 MB.
O `mem_limit: 768m` do compose atual é suficiente. Para atrasos acima de ~150 s, aumente
`MAX_CACHE_MB` e o `mem_limit` na mesma proporção (~1,5 MB por segundo de atraso extra).

## Testes

```bash
cd /mnt/sdb1/hls-relay
bash scripts/run_tests.sh            # unitários + integração + e2e (e2e precisa de ffmpeg)
bash scripts/run_tests.sh --quick    # sem o e2e
python3 -I tests/compare_v03_v04.py /caminho/para/relay_v0.3.py   # comparação v0.3 × v0.4
```

A origem simulada (`tests/hls_sim.py`) roda em `127.0.0.1` e injeta: tokens rotativos,
404, corpo truncado, conexão travada no meio, enlace lento por conexão, playlist atrasada,
erros 500 na playlist, redirect da master, reinício com nomes reaproveitados, reinício sem
tag (só salto de PTS), tag `EXT-X-DISCONTINUITY` da origem e URL de segmento apontando para
host privado (SSRF).

## Voltar para uma versão anterior

Para voltar à versão que estava instalada antes da v0.5 (por exemplo, a v0.4.3):
```bash
sudo bash /mnt/sdb1/hls-relay/scripts/rollback_v03.sh /mnt/sdb1/hls-relay --to v0.4.3
```
Isso restaura o diretório inteiro daquele backup, inclusive sem o `compose.override.yaml`.

### Voltar para a v0.3

```bash
sudo bash /mnt/sdb1/hls-relay/scripts/rollback_v03.sh /mnt/sdb1/hls-relay
# ou um backup específico:
sudo bash /mnt/sdb1/hls-relay/scripts/rollback_v03.sh /mnt/sdb1/hls-relay 20261106-170000
```

O rollback guarda a v0.4 em `/mnt/sdb1/hls-relay.v0.4-<data>`, restaura os arquivos do backup,
**mantém o `.env` atual** (a v0.3 ignora as chaves novas; use `--restore-env` para voltar o
`.env` do backup) e recria só este container. Rollback manual:

```bash
cd /mnt/sdb1
sudo tar -xzf hls-relay-backups/hls-relay-v0.3-AAAAMMDD-HHMMSS.tar.gz -C /tmp
sudo cp /tmp/hls-relay/relay.py /tmp/hls-relay/Dockerfile hls-relay/
sudo rm -f hls-relay/.dockerignore
cd hls-relay && docker compose build && docker compose up -d --force-recreate
```

## O que foi validado e o que depende da transmissão real

**Validado aqui** (Python 3.12 e 3.13, origem simulada local; não houve acesso à sua origem
nem a um daemon Docker):

- 54 testes unitários e de integração (14 do multicanal e da API do painel, inclusive adicionar/remover canais e o modo sob demanda; 9 do atraso e da recomposição da reserva: partida,
  manutenção ≥ 120 s por 100 segmentos, queda de origem de 120 s sem a playlist parar, teto de
  150 s) e 3 testes ponta a ponta com H.264 + AC-3 reais gerados
  pelo ffmpeg, decodificados por um cliente ffmpeg lendo `/live.m3u8`;
- integridade: nenhum byte parcial ou estranho publicado; ordem estrita; nenhuma duplicata;
- consistência da playlist em todas as recargas (MEDIA-SEQUENCE, DSN, TARGETDURATION, borda);
- correção dos 8 cenários em que a v0.3 perde ou duplica segmentos (tabela no DIAGNOSTICO.md);
- servidor HTTP: cliente lento não bloqueia os demais; HEAD; allowlist; nada sensível no `/health`;
- logs sem `token=` nem diretórios;
- scripts de instalação com `docker` simulado (backup, tag, `.env`/compose intactos).

**Depende da transmissão real:**

- se a vazão da origem até o BoxServer comporta 11,7 Mbit/s (rode `--probe` e observe
  `ingest_ratio_60s`). Se não comportar, o relay vai **mostrar** o problema, mas não tem como
  resolvê-lo;
- se 2 conexões são aceitas pelo provedor e realmente aumentam a vazão;
- se a origem rotaciona tokens, reinicia a numeração ou corta segmentos fora de IDR;
- o comportamento do VLC, da Fire TV e do Dispatcharr com 2 min de atraso cadenciado;
- o build da imagem no seu Docker (o `Dockerfile` só ganhou `HEALTHCHECK`, `EXPOSE` e
  variáveis de ambiente);
- o desempenho do Tailscale até cada cliente.

## Limitações

- Somente HLS com MPEG-TS, sem DRM, `AES-128`, fMP4 ou byte-range.
- Com áudio em playlist separada (`#EXT-X-MEDIA`), só a variante é retransmitida (há aviso no log).
- O cache é volátil: reiniciar o container zera o buffer.
- Se a origem ficar sem entregar por mais tempo que colchão + buffer do cliente, o player
  congela. Isso é uma falha real da origem, e aparece em `status` e nos logs.
