# Câmeras IP — 04. Testes, evidências e desempenho

Legenda: **CONFIRMADO** (testado com resultado real) · **IMPLEMENTADO, NÃO TESTADO
NA CÂMERA** · **NÃO SUPORTADO** (provado) · **PENDENTE** (exige senha/ação humana).

## Ambientes

| Ambiente | Uso |
|---|---|
| Máquina da LAN da câmera (BigLinux, kernel 7.2, Xvfb privado) | Câmera real 192.168.15.15 |
| Bancada RTSP (`tests/ipcam/rtsp_bench.py`, gst-rtsp-server) | Mesmo layout observado na Yoosee: `/onvif1` 1280×720 H.264 30 fps, `/onvif2` 640×360, G.711 A-law 8 kHz, Digest realm `HIipCamera`. Acessada da estação via Tailscale (~39 ms RTT) e via túnel SSH |
| Servidor ONVIF falso (`tests/ipcam/test_ipcam.py`) | Reproduz XAddrs embaralhados, `RelayOutputs=0`, conexão fechada sem WSSE válido, PTZ e presets |
| Estação de desenvolvimento (Manjaro, Xvfb privado, renderização por software) | Testes gráficos e de desempenho |

A bancada **não é a câmera**: ela comprova o caminho de software com o mesmo
formato de stream. Os resultados na câmera real estão separados.

## Resultados com a câmera real

| Teste | Resultado | Estado |
|---|---|---|
| 1. Conectividade | ping, ARP com MAC correto, portas 554/5000 | CONFIRMADO |
| 2. Protocolo | RTSP Digest + ONVIF Profile S, `/onvif1` e `/onvif2` | CONFIRMADO |
| Descoberta no BigCam (alvo 192.168.15.15) | Encontrada e identificada como Yoosee em 3,7 s; caminhos preenchidos | CONFIRMADO |
| Teste de conexão no assistente | "Exige usuário e senha"; chips 🔒 para vídeo/PTZ/IR/eventos, "—" para relés/luz/sirene/conversa/SD | CONFIRMADO |
| Janela do BigCam com a câmera | Estado "Senha incorreta" com orientação (sem reconexão infinita) | CONFIRMADO |
| 3. Imagem / 4. vídeo no BigCam | Exige a senha NVR | **PENDENTE** |
| 5. PTZ, IR, eventos na câmera | Exige a senha NVR | **PENDENTE** |
| Relés, conversa | `RelayOutputs=0`, `AudioOutputs=0` | NÃO SUPORTADO |
| Luz, sirene, SD | Sem API local documentada | NÃO SUPORTADO (localmente) |

Capturas: `docs/img/ipcam-real-descoberta.png`, `docs/img/ipcam-real-senha.png`.

Para concluir na câmera real, com a senha NVR definida no app:

```bash
BIGCAM_TEST_RESULTS=$PWD/out REAL_HOST=192.168.15.15 REAL_USER=admin REAL_PASS='…' \
xvfb-run -a -s '-screen 0 1440x900x24' dbus-run-session -- python3 tests/ipcam/run_graphical_real.py
```

(defina `REAL_PASS` sem deixá-la no histórico do shell, por exemplo com `read -s REAL_PASS; export REAL_PASS`).

## Resultados na bancada (BigCam real, sessão gráfica privada)

`tests/ipcam/run_graphical_ip.py` — 14/14 verificações aprovadas:

| Verificação | Resultado |
|---|---|
| Migração legada remove a senha do arquivo de configuração | aprovado |
| Prévia ao vivo 1280×720 | aprovado (TTFF 602 ms na GUI) |
| Painel IP exibido | aprovado |
| Foto 1280×720 | aprovado |
| Gravação local (recorder existente) decodifica sem erros | aprovado |
| Troca principal → secundário (640×360) | aprovado |
| Queda do servidor detectada ("Reconectando") | aprovado |
| Reconexão automática após o servidor voltar | aprovado (0,6 s após restaurar) |
| PTZ: segurar → ContinuousMove; soltar → Stop | aprovado |
| PTZ: Stop ao descartar o painel durante movimento | aprovado |
| Mosaico com 2 blocos ao vivo | aprovado |
| Assistente reporta senha errada | aprovado |
| Assistente com senha certa mostra prévia | aprovado |
| Tema claro / janela estreita | renderizado |

Capturas: `docs/img/ipcam-bancada-ao-vivo.png`, `…-ptz.png`, `…-mosaico.png`.

## Gravação de vigilância (passthrough)

| Teste | Resultado |
|---|---|
| 27 s com segmentos de 0,2 min | 3 arquivos MKV (11,0 s / 12,0 s / 3,7 s), H.264 1280×720 + `pcm_alaw` 8 kHz, **0 erros** ao decodificar com ffmpeg, último segmento finalizado |
| Monitor: detecção local + gravação por movimento | 5 eventos em 25 s respeitando intervalo de 5 s; gravação iniciou 1,8 s após o 1º evento; desligar o modo parou e finalizou o arquivo (23 s, decodificável) |

## Testes automatizados

```bash
python3 -m pytest -q tests          # 137 testes (115 existentes + 22 de câmeras IP)
```

`tests/ipcam/test_ipcam.py` cobre: ONVIF público/protegido/credencial errada/certa;
reescrita de XAddr (host e porta) e `RelayOutputs`/`AudioOutputs`; capacidades
com e sem senha; identificação Yoosee; PTZ (watchdog sem keepalive, renovação,
fallback de Stop, presets); sonda RTSP Digest (ok/errada/vazia, inclusive o 400
da Yoosee); migração legada; validação de host/caminho; redação; limites de
descoberta; agenda e retenção.

## Regressão

| Item | Resultado |
|---|---|
| Suíte existente (115) | aprovada |
| `tests/integration/run_graphical.py` (HTTP/MJPEG, foto, snapshot de rede, gravação, galeria, fechamento) | aprovado sob Xvfb; **etapa AT-SPI pulada** (pyatspi não instalado na estação) |
| Webcam USB / DSLR / smartphone / câmera virtual em hardware | **PENDENTE**: não havia dispositivo livre (a webcam da máquina remota estava em uso pelo BigCam instalado do proprietário e não foi tocada; a estação não tem v4l2loopback carregado). A lógica desses backends não foi alterada. |

## Desempenho

| Medida | Antes | Depois | Ambiente |
|---|---|---|---|
| Conversão BGRA→BGR por frame 720p | 4,20 ms | 0,07 ms | microbenchmark |
| Prévia na GUI, 720p30 (CPU do processo) | 163% | 142,5% | Xvfb + renderização cairo **por software** (não representativo de GPU) |
| Pipeline de prévia sem GUI, 720p30 | 13,7% | 13,5% (mesmo custo, agora sem senha na URL) | bancada |
| Substream 640×360 sem GUI | — | 4,3% CPU | bancada |
| Áudio ligado | — | +~0% (11,8% total) | bancada |
| Tempo até 1º frame (GUI) | 615 ms | 602 ms | bancada via Tailscale |
| Gravação passthrough 720p + áudio | — | 1,8% CPU, ~150 KB/s (~0,5 GB/h) | bancada |
| Detecção local + gravação por movimento | — | 4,1% CPU | bancada |
| Reconexão após o servidor voltar | sem reconexão | 0,6 s | bancada |
| Sonda RTSP (teste de conexão) | — | 110–170 ms | bancada |
| Descoberta (1 IP) / sondagem de capacidades | — | 3,7 s / 3,6 s | câmera real |
| Memória (RSS máx. do app com prévia) | 435 MB | 434 MB | Xvfb |

Na GUI por software, a maior parte da CPU vem do upload e da escala das texturas
pelo renderizador cairo; o pipeline em si custa ~13,5%. Metas para a câmera
real (720p30 H.264, decodificação por software): prévia ≤ 20% de um núcleo fora
da renderização, TTFF ≤ 1,5 s em LAN, reconexão ≤ 2 s após a volta da rede.
