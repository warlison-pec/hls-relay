# Diagnóstico técnico — BoxServer HLS Relay v0.3

Auditoria do `relay.py` v0.3 (338 linhas), `Dockerfile`, `compose.yaml` e `.env.example`.
Cada defeito marcado **confirmado** foi reproduzido numa origem HLS simulada
(`tests/hls_sim.py`, `tests/repro_v03.py`, `tests/compare_v03_v04.py`), rodando o código
original da v0.3 sem alterações. **Hipóteses** dependem de dados da transmissão real e
trazem a forma de verificar.

## 1. Conclusão em uma frase

Os travamentos não vêm do tamanho do buffer. Vêm de dois tipos de problema. O primeiro: o
coletor da v0.3 é um laço único e serial, e em várias condições comuns de fontes instáveis
ele coleta **menos de 1 segundo de vídeo por segundo**. Quando isso acontece, nenhuma janela
ou cache evita o congelamento. O segundo: há erros de lógica HLS que fabricam duplicatas,
descontinuidades e perdas que a origem nunca teve.

## 2. O que os próprios números relatados já provam

| Observação | Cálculo | Leitura |
|---|---|---|
| `IncompleteRead(3888444 bytes read, 421644 more expected)` | 3.888.444 + 421.644 = **4.310.088 bytes** por segmento | Com segmentos de ~2,94 s (valor usado nos testes da v0.3), dá **11,7 Mbit/s**. Bate com o `BANDWIDTH` anunciado (10–12 Mbit/s). A conexão caiu com 90 % do arquivo baixado. |
| 4 segmentos por lote a cada 15–32 s | 4 × 2,94 s = 11,7 s de vídeo a cada 15–32 s | A coleta roda a **0,37×–0,78× o tempo real**. O player esvazia o buffer e congela, com qualquer cache ou janela. |
| Origem com 4 segmentos na janela (hipótese coerente com o “lote de 4”) | 4 × 2,94 s ≈ 11,7 s | Qualquer pausa do coletor acima de ~11,7 s perde segmentos, porque eles saem da janela da origem. Isso gera **404**, lacunas e `EXT-X-DISCONTINUITY`. |

Para acompanhar 11,7 Mbit/s com folga, o relay precisa de **≥ 14–15 Mbit/s sustentados** da
origem até o BoxServer. Na v0.3, isso precisa sair de **uma única conexão**, já que tudo é
serial. E ainda é preciso descontar o tempo gasto com playlists, retries e o `sleep`.

## 3. Defeitos confirmados na v0.3 (com reprodução)

Tempo da simulação escalado em 1/3: segmentos de 1 s representam os de ~3 s, `POLL` vira 0,67 s
e `HTTP_TIMEOUT` vira 6,7 s. A janela da origem tem 4 segmentos.

| # | Onde (v0.3) | Defeito | Reprodução v0.3 → v0.4 |
|---|---|---|---|
| 1 | `worker()` L288-298, `update()` L218-244 | **Pipeline serial.** Um único thread resolve a master, baixa a media playlist, baixa os segmentos um a um, faz os retries com `sleep` e só então dorme `POLL`. O ciclo dura a soma de tudo isso. | Conexão a 0,8× do bitrate: **10 de 40 perdidos, 6 descontinuidades** → 0 perdidos, 0 desc. |
| 2 | `fetch()` L77-83 + `HTTP_TIMEOUT=20` | **Uma leitura parada bloqueia o relay inteiro por até 20 s.** É mais que a janela da origem (~11,7 s). Não existe prazo total por download. | Uma conexão travada: **3 perdidos + 1 descontinuidade** → 0 / 0 |
| 3 | `update()` L219 + `identity()` L179-180 + `save_segment()` L191-193 | A master é **resolvida de novo a cada ciclo**, e a identidade do segmento inclui a URL da media playlist e a URL do segmento **com a query string**. Quando a origem rotaciona tokens, cada ciclo vê segmentos “novos”: baixa a janela inteira de novo, publica vídeo repetido e marca descontinuidade (`media != last_source_media`). | Tokens rotativos: **168 downloads para 30 segmentos, 138 duplicados, 41 descontinuidades** → 30 / 0 / 0 |
| 4 | `save_segment()` L187 (`seen` com 1.000 entradas ≈ 49 min) | **Reinício da origem que reaproveita nomes e números** (`seg-0.ts`…) é tratado como “já visto” e descartado em silêncio, sem descontinuidade. | **12 de 15 segmentos pós-reinício perdidos** → 0 |
| 5 | `parse_media_playlist()` L158-175 | **`#EXT-X-DISCONTINUITY` da origem é ignorado.** Quando a origem troca encoder ou timestamps, o player não recebe o aviso. | Tag da origem com salto de PTS: **0 tags publicadas** → 1 |
| 6 | `fetch()` L83 descarta `geturl()`; `resolve_media_playlist()` L128; `parse_media_playlist()` L169 | **URLs relativas resolvidas contra a URL anterior ao redirect.** Se a origem redireciona (balanceador/edge), as variantes e os segmentos apontam para o host errado (404). | Master com 302: **0 segmentos** → 20 / 20 |
| 7 | `chosen_segments()` L254 | **A borda ao vivo recua na partida.** Com até 6 segmentos, tudo é publicado. No 7º, os 4 últimos são escondidos e o último item anunciado volta de 5 para 2. Isso viola o HLS (a playlist só pode crescer no fim) e o player espera um segmento que “sumiu”. | Sequência anunciada: `0,1,2,3,4,5,`**`2`**`,3,4,5` → monotônica |
| 8 | `chosen_segments()` | **`START_BEHIND_SEGMENTS` só acrescenta atraso, sem proteção.** O recuo é fixo em relação ao segmento mais novo do cache. Quando a origem para, a playlist publicada para no mesmo instante, e o cliente fica só com o buffer dele. | Demonstrado por análise e pelo teste `PacingTests` (na v0.4 o colchão continua liberando 1 segmento por duração durante a pausa) |
| 9 | `fetch_segment()` L86-103, `update()` L230-234 | Faz retry de **404** (normalmente definitivo) com `sleep` no mesmo thread. Uma falha **aborta o lote inteiro** (bloqueio na cabeça da fila) até o segmento sair da janela da origem. | Um 404: 38 requisições contra 32; sem perda neste cenário (atraso, não perda) |
| 10 | `publish()` L263 | `EXT-X-TARGETDURATION` é recalculado a cada janela e pode mudar entre recargas, o que a especificação proíbe. | Corrigido: valor monotônico |
| 11 | `Handler` L301-332 | HTTP/1.0 sem keep-alive. Sem `HEAD` (responde 501). Sem timeout de socket por cliente. Cada requisição registrada em INFO (ruído). Nenhum controle de acesso na aplicação. | `HttpServerTests` |
| 12 | `parse_media_playlist()` L146 | Rejeita `#EXT-X-KEY:METHOD=NONE`, que é válido e inofensivo. | `ParsingTests` |
| 13 | `state` | `failed_downloads` conta tentativas, não segmentos. Não há categorias de erro, vazão, tempo por segmento nem distinção entre falha de playlist e falha de segmento. | Novo `/health` |

### Itens auditados sem defeito relevante

- **Locks e threads:** o `RLock` nunca é mantido durante a rede, e o `ThreadingHTTPServer`
  atende clientes em paralelo. **Downloads lentos não travam o servidor HTTP.** Eles travam a
  *coleta*, e o efeito chega ao cliente porque a playlist para de crescer.
- **Integridade:** `IncompleteRead` já impedia publicar segmento truncado. O custo estava no
  retry serial, que baixa de novo os 4,3 MB antes de seguir.
- **Memória:** 40 × 4,3 MB ≈ 172 MB, dentro de `MAX_CACHE_MB=512` e `mem_limit: 768m`.
- **Seleção de variante:** correta para uma variante. Com `#EXT-X-MEDIA` de áudio separado,
  o áudio se perderia (a v0.4 detecta e avisa).

## 4. Hipóteses que dependem da transmissão real

| Hipótese | Como verificar (v0.4) |
|---|---|
| **H1. Vazão origem→BoxServer insuficiente**, por conexão ou no total | `docker exec boxserver-hls-relay python /app/relay.py --probe 4`: compara Mbit/s por conexão, 2 em paralelo e bitrate. No `/health`: `avg_realtime_factor` < 1,2 e `ingest_ratio_60s` < 1,0. |
| **H2. A origem rotaciona tokens** (explicaria o “lote de 4” em todo ciclo da v0.3) | Antes de instalar, no `/health` da v0.3: se `discontinuities` sobe ~1 por ciclo e `downloaded` sobe de 4 em 4, H2 está confirmada. Na v0.4 não há efeito (identidade = sequência + nome do arquivo). |
| **H3. A origem lista segmentos que não serve**, ou reinicia a numeração | Log `event=segment_failed ... listed=True` (a origem anunciou e não entregou) contra `listed=False` (o relay chegou tarde). `event=source_reset` com o motivo. |
| **H4. Segmentos que não começam em IDR** | `keyframe_start_ratio` < 1 no `/health`. Nesse caso, toda entrada no canal, lacuna ou descontinuidade gera `non-existing PPS` até o próximo keyframe. |
| **H5. Gargalo do lado do cliente** (Tailscale via DERP, Wi-Fi da Fire TV) | `clients.recent[].segments_behind_live_edge` crescendo e `segment_404` > 0. `tailscale status` / `tailscale ping <ip>` deve mostrar conexão `direct`, não `relay "xxx"`. |

## 5. Sobre `non-existing PPS 0 referenced`, `decode_slice_header error`, `no frame!`

São mensagens do decodificador H.264 do FFmpeg/VLC quando ele recebe fatias de vídeo antes de
ter visto SPS/PPS e um quadro IDR. Ou seja, a decodificação começou ou recomeçou no meio de
um GOP. Isso acontece:

1. ao entrar no canal, se o segmento inicial não começa em IDR;
2. depois de **lacunas** (segmento perdido) e de **descontinuidades** (o decodificador é reiniciado);
3. depois de **conteúdo duplicado ou fora de ordem** (defeito 3).

São **consequência**, não causa. Na v0.3, os defeitos 1–5 criam exatamente essas situações.
Com os segmentos reais do ffmpeg (alinhados em keyframe), a v0.4 decodificou 20 s ao vivo
**sem nenhuma mensagem de erro** (`tests/test_e2e_ffmpeg.py`).

## 6. Os 10 pontos de auditoria pedidos

| Ponto | v0.3 | v0.4 |
|---|---|---|
| 1. Master e variante | Re-resolvida a cada ciclo; base pré-redirect | Resolvida uma vez; de novo só após 401/403/404/410 ou playlist inválida (ou `MASTER_REFRESH_SECONDS`); base = URL final; aviso de áudio alternativo |
| 2. Frequência da media playlist | `POLL` depois de um ciclo inteiro (ciclo de 15–32 s observado) | `min(POLL_INTERVAL, TARGETDURATION/2)`, contado a partir do início da recarga, em thread próprio |
| 3. Download e integridade | Serial, conexão nova por requisição, timeout de 20 s por leitura | 2 workers com keep-alive, `Content-Length` conferido, prazo total, timeout de leitura de 6 s (automático), validação MPEG-TS (sync, tamanho múltiplo de 188, PAT/PMT) |
| 4. Ordem e continuidade | Ordem de chegada | Commit estrito na ordem da origem; downloads paralelos não reordenam |
| 5. Reinícios / descontinuidades | Por número de sequência; ignora a tag da origem | Detecção de reinício (sequência recua ou nome muda para a mesma sequência), tag da origem propagada e decisão final pelo **PTS real** |
| 6. Playlist local | Borda recua; `TARGETDURATION` variável | Sequência local monotônica (inclusive entre reinícios do container), DSN coerente, borda nunca recua, `TARGETDURATION` monotônico |
| 7. Retenção / memória | Só contagem e bytes | Janela publicada protegida, graça por tempo após sair da playlist, teto rígido de bytes com aviso |
| 8. Clientes simultâneos | OK (`ThreadingHTTPServer`) | Idem + HTTP/1.1 keep-alive, `HEAD`, timeout de 30 s por socket, lista de redes permitidas |
| 9. Downloads lentos × HTTP | Não bloqueiam o HTTP; bloqueiam a coleta | Não bloqueiam nem o HTTP nem os outros downloads |
| 10. Vazão real × bitrate | Não medida | `avg_download_mbps`, `source_bitrate_mbps`, `avg_realtime_factor`, `ingest_ratio_60s`, `--probe` |

## 7. Comparação v0.3 × v0.4 na mesma origem simulada

`python3 -I tests/compare_v03_v04.py /caminho/relay_v0.3.py`, segmentos de 1 s, janela de 4.
Colunas: segmentos perdidos / duplicados publicados / descontinuidades / downloads feitos.

| Cenário | v0.3 | v0.4 |
|---|---|---|
| Normal | 0 / 0 / 0 / 30 | 0 / 0 / 0 / 30 |
| Tokens rotativos na URL | 0 / **138** / **41** / **168** | 0 / 0 / 0 / 30 |
| Um segmento 404 | 0 / 0 / 1 / 38 | 0 / 0 / 1 / 32 |
| Reinício reaproveitando nomes | **12** / 0 / **0** / 18 | 0 / 0 / 1 / 33 |
| Master com redirect + URL relativa | **20 (tudo)** / 0 / 0 / 0 | 0 / 0 / 0 / 20 |
| Uma conexão travada no meio | **3** / 0 / 1 / 28 | 0 / 0 / 0 / 31 |
| Enlace a 1,18× o bitrate | 0 / 0 / 0 / 40 | 0 / 0 / 0 / 40 |
| Cada conexão a 0,8× o bitrate | **10** / 0 / **6** / 30 | 0 / 0 / 0 / 40 |
| Corpo truncado 2× em 2 segmentos | 0 / 0 / 0 / 34 | 0 / 0 / 0 / 34 |

Limite honesto do último ganho: dois downloads em paralelo só ajudam se o limite de
velocidade for **por conexão**. Se o enlace total for mais lento que o bitrate, nenhum relay
resolve. O `--probe` e o `/health` mostram em qual caso a sua origem está.
