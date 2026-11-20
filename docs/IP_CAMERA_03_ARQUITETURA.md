# Câmeras IP — 03. Arquitetura, protocolos, streaming, PTZ, alertas, gravações, segurança e UX

## Visão geral

```
ui/ip_camera_dialog.py      Assistente (descobrir / manual / testar / salvar)
ui/ip_camera_panel.py       Painel lateral: PTZ, qualidade, áudio, luz/alarme, alertas, gravação, dispositivo
ui/ip_camera_wall.py        Mosaico 2×2 (substreams) + histórico de eventos
ui/ip_camera_controller.py  Cola com a janela: persistência, fluxos, notificações, monitor
        │
core/backends/ip_backend.py Backend do BigCam (fonte GStreamer, controles ONVIF Imaging, foto)
core/stream_engine.py       Prévia + reconexão + watchdog (já existente, estendido)
        │
core/ipcam/  (sem GTK; só stdlib + GStreamer/numpy onde necessário)
  onvif.py         Cliente SOAP 1.2: WS-UsernameToken, Digest, reescrita de XAddr
  rtsp.py          Sonda OPTIONS/DESCRIBE com Basic/Digest + parser SDP
  discovery.py     WS-Discovery + sondagem limitada da LAN
  capabilities.py  O que o dispositivo realmente suporta (estado por recurso)
  models.py        IPCameraConfig (sem senhas) + migração do formato antigo
  security.py      CredentialStore (libsecret), validação, redação de logs
  ptz.py           PTZController com parada garantida
  events.py        PullPoint ONVIF, detecção local de movimento, histórico
  recorder.py      Gravação passthrough segmentada, retenção, agenda
  monitor.py       Serviço em segundo plano por câmera (eventos + gravação)
  yoosee.py        Adaptador: diferenças comprovadas da Yoosee
```

Nenhum módulo de `core/ipcam` importa GTK. Toda E/S de rede roda em worker
(`utils.async_worker.run_async`) ou em thread dedicada com timeout explícito.

## Protocolos

* **ONVIF** — implementação própria, sem dependências novas (o `python-onvif-zeep`
  traria zeep/lxml e não lida com os XAddrs incorretos da Yoosee). WS-Security
  UsernameToken com PasswordDigest, compensação de relógio
  (`GetSystemDateAndTime`), fallback HTTP Digest, respostas limitadas a 4 MiB,
  falhas classificadas (`OnvifAuthError`, `OnvifDenied` = conexão fechada sem
  resposta, `OnvifFault`). Host **e porta** anunciados são substituídos pelos
  configurados (NAT/túneis).
* **RTSP** — vídeo via `rtspsrc`; uma sonda própria (`rtsp.py`) é usada no
  teste de conexão e para classificar falhas antes do primeiro frame.
* **HTTP/MJPEG** — `souphttpsrc` + `multipartdemux single-stream`.

## Streaming

Fragmento gerado (credenciais **não** aparecem na string):

```
rtspsrc name=ipsrc location="rtsp://host:554/onvif1" protocols=tcp latency=200
        drop-on-latency=true do-rtsp-keep-alive=true tcp-timeout=10000000 timeout=5000000 retry=5
  [ipsrc. ! application/x-rtp,media=audio ! queue ! decodebin ! audioconvert ! audioresample
          ! volume name=ipvolume mute=true ! autoaudiosink sync=false]
  ipsrc. ! application/x-rtp,media=video ! queue ! decodebin ! videoconvert
```

* `user-id`/`user-pw` são definidos no elemento após o parse (`prepare_pipeline`).
* `decodebin` escolhe decodificador por *rank*: VA-API/NVDEC quando instalados,
  software (`avdec_h264`) como fallback. Nas máquinas testadas não havia
  decodificador por hardware instalado (`vah264dec`/`nvh264dec` ausentes).
* Principal/secundário, TCP/UDP/automático e buffer configuráveis por câmera.
* Áudio: desligado (mudo) por padrão; o painel liga/desliga sem reiniciar. Se não
  houver saída de áudio, o vídeo continua sem áudio.
* Câmera virtual/OBS: a câmera IP usa o mesmo caminho appsink → `v4l2loopback`
  dos demais backends; em segundo plano o ramo de áudio é sempre omitido.

### Reconexão

| Situação | Comportamento |
|---|---|
| Erro antes do 1º frame | Sonda RTSP própria: senha recusada → "Senha incorreta" (sem novas tentativas); caminho inexistente → mensagem e para; rede → backoff |
| Erro/EOS após vídeo | Backoff 1, 2, 4, 8, 16, 30, 30… s; estado "Reconectando em Ns (tentativa n)" |
| Sem frames por 10 s | Watchdog dispara a reconexão (cobre TCP "pendurado") |
| Usuário troca/para a câmera | Timer cancelado; contador zerado no 1º frame ou na troca |

## PTZ físico

`ContinuousMove` com `Timeout` ONVIF de 1,5 s, renovado a cada 0,8 s enquanto o
botão está pressionado. Camadas de segurança:

1. Timeout no próprio comando (a câmera para sozinha se o BigCam sumir).
2. Watchdog local: sem *keepalive* da interface por 2 s → `Stop`.
3. Limite absoluto de 30 s de movimento contínuo.
4. `Stop` ao soltar, ao perder o foco, quando a janela fica inativa, ao trocar de
   câmera, ao fechar o painel ou o app. Se a câmera recusar `Stop`, envia
   `ContinuousMove` com velocidade zero (comportamento de firmwares Yoosee).

Comandos são enfileirados numa thread dedicada ("o último vence"), sem acúmulo
em câmeras lentas. Presets (`GetPresets/SetPreset/GotoPreset/RemovePreset`) e
`GotoHomePosition` só aparecem se o dispositivo os confirmar. Zoom óptico só
aparece se `GetNodes` anunciar espaço de zoom.

**PTZ físico × digital:** o painel "Movimento físico (PTZ)" move motores; o zoom
e o enquadramento digitais continuam na barra da prévia, e o texto do painel
deixa isso explícito.

## Luz, IR, sirene

* **IR-cut / visão noturna**: ONVIF Imaging `IrCutFilter` (AUTO/ON/OFF) exposto
  como controle "Visão noturna" na UI genérica de controles; brilho, contraste,
  saturação e nitidez também, se a câmera os reportar.
* **Sirene/luz por relé**: `SetRelayOutputState` com confirmação, pulso de 5 s e
  desligamento garantido (`finally`). Só aparece se `RelayOutputs > 0`.
* **Yoosee**: luz branca, sirene e conversa não têm API local documentada →
  aparecem como indisponíveis, com o motivo. Nada é simulado.

## Alertas e eventos

* Nativos: assinatura ONVIF PullPoint (renovada a cada ~100 s, backoff em falhas).
* Locais (rotulados "detecção do BigCam"): substream decodificado a 2 fps e
  320×180 em escala de cinza; diferença de quadros com fundo adaptativo, erosão
  2×2 e exigência de 2 quadros consecutivos; sensibilidade 1–100.
* Intervalo mínimo entre alertas configurável; notificações `Gio.Notification`
  com ID por câmera (substituem a anterior, não roubam foco).
* Foto do evento salva em `<gravações>/<câmera>/events/<data>/`.
* Histórico em `~/.local/share/bigcam/ipcam-events.jsonl` (permissão 600,
  rotação ao passar de 2 MiB), com filtro por câmera e tipo.

## Gravações

* **Locais do BigCam (vigilância)**: `rtph264depay ! h264parse ! splitmuxsink
  (matroskamux)` + áudio G.711/AAC copiado — **sem recodificação**. Modos: desligado,
  contínuo, por movimento (pós-gravação configurável), agendado (`HH:MM-HH:MM`,
  janelas que cruzam meia-noite). Segmentos de N minutos em
  `~/Vídeos/BigCam/IP/<câmera>/<AAAA-MM-DD>/<HH-MM-SS>.mkv` (diretórios 700).
  Para abaixo de 1 GiB livre; retenção em dias apaga só segmentos `.mkv` próprios.
* **Gravação manual com efeitos**: o botão de gravar existente continua
  funcionando para câmeras IP (recodifica, como nos outros backends).
* **Gravações da câmera (SD)**: só via ONVIF Recording/Search quando houver; na
  Yoosee aparece como indisponível com o motivo.

## Segurança

* Senhas no Secret Service (libsecret, esquema `br.com.biglinux.bigcam.IPCamera`,
  atributo `camera-id`). Sem chaveiro, a senha fica só em memória e a UI avisa.
* Configuração (`ip_cameras`) sem segredos; entradas antigas com `user:senha@`
  são migradas automaticamente e o arquivo é reescrito sem a senha.
* Host validado (IP literal ou hostname RFC 1123), porta 1–65535, caminho sem
  espaços/aspas/barra invertida; valores entram em pipelines via `gst_quote`;
  nenhum shell é usado.
* Descoberta: só redes privadas da própria máquina, no máximo um /24 por
  interface (ou /22 digitado), timeouts de 0,5 s, nunca testa credenciais.
* Redação de credenciais em logs (`redact`); a URL nunca é registrada com senha.
* Falha de uma câmera não derruba as outras nem o app (erros isolados por
  pipeline/monitor).

## UX

* Assistente com duas entradas ("Encontrar automaticamente" / "Adicionar
  manualmente"), progresso, resultados com fabricante/modelo/protocolos, dica
  específica para Yoosee, teste com prévia real e chips de capacidade
  (✓ suportado, 🔒 exige senha, — indisponível) com tooltips.
* Painel com estado da conexão (Conectando / Conectado / Reconectando / Senha
  incorreta / Transmissão não encontrada).
* Controle PTZ: 3×3 com diagonais, segurar para mover, setas e teclado numérico
  com foco, Esc/Espaço param, rótulos acessíveis, estados hover/pressed/focus,
  respeita "reduzir movimento".
* Mosaico 2×2 paginado, favoritos primeiro, duplo clique amplia (os outros
  blocos param de decodificar), Esc volta.
* Tudo traduzível via gettext; pt-BR completo.

## Compatibilidade

| Tipo | Suporte |
|---|---|
| ONVIF genérico (Profile S) | Vídeo, áudio, PTZ, presets, Imaging/IR-cut, eventos, relés — conforme o dispositivo confirmar |
| RTSP genérico | Vídeo/áudio, gravação passthrough, detecção local |
| HTTP/MJPEG | Vídeo, detecção local (gravação passthrough não se aplica) |
| Yoosee/Gwell | Como ONVIF + correções de XAddr; limitações documentadas |
| H.265 | Prévia via `decodebin`; gravação passthrough com `h265parse` |
