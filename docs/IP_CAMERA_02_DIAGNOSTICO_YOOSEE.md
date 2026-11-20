# Câmeras IP — 02. Diagnóstico da câmera Yoosee "Garagem"

Data: 20/11/2026. Testes feitos a partir de uma máquina Linux autorizada na
mesma LAN da câmera. Todas as sondagens foram limitadas ao endereço
autorizado; nenhuma senha foi adivinhada (uma única tentativa com senha vazia
foi feita por indicação do proprietário).

## Identificação

| Item | Valor | Fonte |
|---|---|---|
| Endereço | 192.168.15.15 (MAC c8:fe:0f:a0:ba:39 confirmado via `ip neigh`) | Rede |
| ID Yoosee | 6175584333 (etiqueta; campo **PWA** — senha de fábrica — em branco; fabricação 24/04) | Etiqueta/app |
| ONVIF `GetDeviceInformation` | Manufacturer `Technology`, Model `IPC`, Firmware `33.00.25`, Serial `0000000`, HardwareId `Ver 2.1` | Câmera |
| Servidor ONVIF | gSOAP/2.8, TCP **5000**, `/onvif/device_service` | Câmera |
| Fabricante real | Plataforma Gwell/Yoosee (OEM genérico). O modelo comercial exato não é exposto pelo dispositivo. | — |

## Conectividade

* Máquina de teste: Wi‑Fi `192.168.15.19/24` e cabo `192.168.15.20/24`, rota
  direta para 192.168.15.15. `ping` com RTT 3–43 ms (Wi‑Fi da câmera).
* **A estação onde o BigCam foi desenvolvido não alcança 192.168.15.15**
  (outra rede; acesso só via Tailscale à máquina de teste). Ver "Acesso remoto".

## Matriz de portas (TCP, somente o endereço autorizado)

| Porta | Estado | Serviço |
|---|---|---|
| 554 | aberta | RTSP (Digest, realm `HIipCamera`) |
| 5000 | aberta | ONVIF (gSOAP 2.8) |
| 80, 81, 443, 1935, 8000, 8080, 8554, 8899, 8999, 9000, 34567, 37777 | fechadas | — |
| UDP 3702 (WS-Discovery) | **sem resposta** (multicast e unicast) | — |

## RTSP

```
OPTIONS → 200, Public: OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE,
          GET_PARAMETER, SET_PARAMETER, USER_CMD_SET   ← método proprietário
DESCRIBE sem credenciais → 401, WWW-Authenticate: Digest realm="HIipCamera"
DESCRIBE com Digest correto para admin/senha vazia → 400 Bad Request (sem CSeq)
```

* O `400` sem `CSeq` faz o GStreamer reportar só *"Could not receive message
  (Parse error)"*. Duas implementações independentes (ffprobe e o cliente do
  BigCam) receberam o mesmo `400` → a câmera **tem senha definida**.
* Caminhos (via ONVIF `GetStreamUri`): `/onvif1` (principal) e `/onvif2`
  (secundário). `InvalidAfterConnect=true`.

## ONVIF

| Operação | Sem credencial | Observação |
|---|---|---|
| GetDeviceInformation, GetCapabilities, GetServices, GetProfiles, GetStreamUri, GetVideoSources, GetVideoEncoderConfigurationOptions | **responde** | Públicas neste firmware |
| GetNodes/GetConfigurations/GetStatus/GetPresets (PTZ), GetImagingSettings, GetEventProperties, GetRelayOutputs, GetSnapshotUri, GetSystemDateAndTime*, GetUsers, GetRecordingSummary | conexão fechada sem resposta | Comportamento do firmware para operações protegidas |
| GetConfigurationOptions (PTZ) | Fault "not implemented" | Não suportado |

\* `GetSystemDateAndTime` respondeu uma vez e depois passou a ser recusado.

Perfis:

| Token | Nome | Vídeo | Áudio | PTZ |
|---|---|---|---|---|
| IPCProfilesToken0 | MainStream | H.264 1280×720, até 30 fps, 1200 kbps | G.711 | PTZConfigToken |
| IPCProfilesToken1 | SubStream | H.264 640×360, até 30 fps | G.711 | PTZConfigToken |

`GetCapabilities` declara: `RTP_TCP=true`, `RTP_RTSP_TCP=true`,
`RTPMulticast=false`, **`AudioOutputs=0`**, **`RelayOutputs=0`**,
`AudioSources=1`, `WSPullPointSupport=true`.

**Peculiaridade do firmware:** os XAddrs vêm embaralhados — Media aponta para
`ptz_service`, PTZ para `deviceio_service`, Events para `media_service` e
DeviceIO para um IP antigo (`192.168.1.33`). O gSOAP despacha pela ação SOAP,
então o BigCam usa `device_service` para tudo quando identifica a Yoosee.

## Capacidades — classificação

| Recurso | Estado | Evidência |
|---|---|---|
| Vídeo H.264 principal/secundário | **PENDENTE (senha)** | Perfis e URIs confirmados; RTSP exige Digest |
| Áudio da câmera (G.711) | **PENDENTE (senha)** | Perfil anuncia G.711; SDP só após autenticação |
| PTZ (pan/tilt) | **PENDENTE (senha)** | Perfis têm `PTZConfiguration`; operações PTZ exigem autenticação |
| Presets | **PENDENTE (senha)** | Depende de `GetPresets` autenticado |
| Zoom óptico | **PENDENTE** | Não é possível ler `GetNodes` sem senha |
| Infravermelho / IR-cut | **PENDENTE (senha)** | Imaging exige autenticação |
| Eventos ONVIF (movimento) | **PENDENTE (senha)** | PullPoint anunciado; exige autenticação |
| Saídas de alarme (relé) | **NÃO SUPORTADO** | `RelayOutputs=0` declarado pela câmera |
| Áudio bidirecional | **NÃO SUPORTADO** (localmente) | `AudioOutputs=0`; a conversa do app usa P2P proprietário |
| Luz branca / sirene | **NÃO SUPORTADO** (localmente) | Sem API local documentada; integração HACS publicada só altera o estado da interface; o app usa nuvem/P2P |
| Gravações do cartão SD | **NÃO SUPORTADO** (localmente) | Sem serviço ONVIF Recording/Search; doc. do fabricante diz que só o app acessa |
| Descoberta WS-Discovery | **NÃO SUPORTADO** | Sem resposta em 3702; descoberta funciona por sondagem direta |

## Autenticação — o que NÃO confundir

| Credencial | Uso |
|---|---|
| Conta do app Yoosee | Login na nuvem; **não** serve para RTSP/ONVIF |
| Senha do dispositivo / "Conexão NVR" | **É a usada pelo BigCam** (usuário `admin`; o guia NVR cita `administrator`) |
| SSH da máquina de teste | Só para diagnóstico; nunca armazenada no repositório |

**Ação necessária do proprietário:** no app Yoosee, *Garagem → ⚙ Configurações →
Conexão NVR → Definir senha*. Depois, no BigCam: Menu → Adicionar câmera IP →
Encontrar automaticamente → Garagem → digitar a senha → Testar conexão.

## Acesso remoto reproduzível

A máquina com o BigCam precisa alcançar 192.168.15.15. Opções, da melhor para a pior:

1. **Rodar o BigCam numa máquina da própria LAN** (recomendado; descoberta e UDP funcionam).
2. **Rota de sub-rede Tailscale/Headscale**: na máquina da LAN,
   `sudo tailscale up --advertise-routes=192.168.15.0/24` e aprovar a rota no
   painel de controle. Depois o BigCam remoto usa `192.168.15.15` diretamente.
   (Altera a configuração da VPN — exige decisão do proprietário; não foi aplicado.)
3. **Túnel SSH** (validado com a bancada): `ssh -N -L 15554:192.168.15.15:554 -L 15000:192.168.15.15:5000 usuário@máquina-da-lan`
   e cadastrar a câmera como host `127.0.0.1`, porta RTSP `15554`, ONVIF `15000`,
   transporte **TCP**. O BigCam reescreve host e porta anunciados pela câmera
   (XAddrs e URIs) para os configurados, então ONVIF funciona pelo túnel. WS-Discovery
   e RTP/UDP não atravessam túnel TCP.

Nunca exponha 554/5000 da câmera diretamente à internet.

## Fontes consultadas

* Yoosee — RTSP/VLC: https://business.yoosee.com/blog/yoosee-ip-camera-vlc-rtsp
* Yoosee — NVR: https://yoosee.app/how-to-connect-the-camera-with-nvr
* Yoosee — IR: https://yoosee.pro/docs/faqs/live-feed/infrared-light-control
* victorbillyph/Yoosee-camera-documentation (MIT) — realm `HIipCamera`, endpoints ONVIF.
* victorbillyph/Yoosee-hacs-integration (MIT) — PTZ ONVIF; switches de luz/IR sem implementação real.
* zDiazG/yoosee-cam-utils (GPL-3.0+) — padrão ContinuousMove + Timeout + Stop com fallback de velocidade zero (adotado como ideia; código próprio).
* cyyl1990/camera-yoosee-tui — sem LICENSE; menciona geração de senha a partir do ID, **não utilizada** (adivinhação de senha é proibida neste projeto).

Nenhum código externo foi copiado.
