# Câmeras IP — 05. Relatório final

Branch: `feature/ip-cameras-onvif-yoosee`. Documentos de apoio:
[01 Auditoria](IP_CAMERA_01_AUDITORIA.md) ·
[02 Diagnóstico Yoosee](IP_CAMERA_02_DIAGNOSTICO_YOOSEE.md) ·
[03 Arquitetura](IP_CAMERA_03_ARQUITETURA.md) ·
[04 Testes](IP_CAMERA_04_TESTES.md).

## 1. O que foi encontrado na câmera

Câmera da plataforma Gwell/Yoosee em 192.168.15.15 com RTSP (554, Digest,
realm `HIipCamera`, método proprietário `USER_CMD_SET`) e ONVIF Profile S
(gSOAP 2.8 na porta 5000). Streams H.264 1280×720 (`/onvif1`) e 640×360
(`/onvif2`), áudio G.711. Informações, perfis e URIs são públicos; vídeo, PTZ,
imagem e eventos exigem a senha do dispositivo. O firmware anuncia XAddrs
trocados e um IP antigo, não responde ao WS-Discovery e responde credencial
errada com `400` sem `CSeq`.

## 2. Fabricante e modelo

ONVIF informa "Technology / IPC", firmware 33.00.25, hardware "Ver 2.1"
(OEM Gwell/Yoosee, ID 6175584333). O modelo comercial não é exposto.

## 3. Protocolos e portas usados

RTSP/TCP 554 (vídeo e áudio), ONVIF/HTTP 5000 (dispositivo, mídia, PTZ, imagem,
eventos). Sem UDP, sem nuvem, sem protocolo proprietário.

## 4. Como a imagem ao vivo é obtida

`rtspsrc` (TCP, latência 200 ms) com credenciais injetadas no elemento →
`decodebin` (hardware quando disponível) → appsink do BigCam. **Na câmera
real a imagem ainda depende da senha NVR** (ver Pendências). O caminho
completo foi validado com uma bancada com o mesmo formato de stream.

## 5. Integração ao BigCam

Novo subsistema `core/ipcam/` (sem GTK), `IPBackend` reescrito,
`StreamEngine` com reconexão/watchdog/classificação de erros, assistente de
cadastro, painel lateral, mosaico, histórico de eventos e serviço de
monitoramento em segundo plano. A câmera aparece no seletor normal e funciona
com efeitos, foto, gravação e câmera virtual pelo mesmo caminho dos demais
backends.

## 6. Recursos físicos confirmados

| Recurso | Estado |
|---|---|
| Rede, RTSP, ONVIF, perfis principal/secundário, codecs anunciados | CONFIRMADO |
| Exigência de autenticação (vídeo, PTZ, imagem, eventos) | CONFIRMADO |
| Ausência de relés e de saída de áudio | NÃO SUPORTADO (declarado pela câmera) |
| Vídeo/áudio ao vivo, PTZ, presets, IR-cut, eventos | PENDENTE — senha NVR |
| Luz branca, sirene, conversa, SD | NÃO SUPORTADO localmente (sem API documentada) |

## 7. Recursos implementados (válidos para qualquer câmera ONVIF/RTSP)

Descoberta; cadastro/edição/remoção; favoritos e local/grupo; senha no
chaveiro; teste com prévia; principal/secundário; TCP/UDP/automático; áudio com
mudo; reconexão; PTZ físico com presets e posição inicial; zoom óptico (quando
anunciado); IR-cut e ajustes de imagem ONVIF; saídas de alarme com pulso
seguro; eventos ONVIF e detecção local; notificações; histórico; gravação
passthrough contínua/por movimento/agendada com retenção; mosaico 2×2;
captura de foto; câmera virtual/OBS.

## 8. Não disponível e por quê

* **Luz/sirene/conversa/SD na Yoosee**: só pelo protocolo P2P/nuvem do
  fabricante, sem API ou SDK local documentado. Não foi feita engenharia
  reversa nem uso da nuvem.
* **WS-Discovery na Yoosee**: o firmware não responde; a descoberta usa
  sondagem direta.
* **Gravações em nuvem**: sem integração oficial disponível.

## 9. Bugs corrigidos

Senha RTSP em texto puro nas configurações; ausência de reconexão; erros de
autenticação genéricos; `fuser` executado com URL; logs do GStreamer sem
redação; conversão BGRA→BGR 60× mais lenta que o necessário (afetava também
DSLR/gPhoto2); testes que poderiam gravar senhas no chaveiro real.

## 10. Arquivos

Novos: `core/ipcam/{onvif,rtsp,models,security,discovery,capabilities,yoosee,ptz,events,recorder,monitor}.py`,
`ui/ip_camera_{controller,panel,wall}.py`, `tests/ipcam/*`, `docs/IP_CAMERA_*.md`, `docs/img/ipcam-*.png`.
Alterados: `core/backends/ip_backend.py`, `core/stream_engine.py`,
`utils/frame_buffers.py`, `ui/ip_camera_dialog.py`, `ui/window.py`,
`ui/camera_controls_page.py`, `ui/preview_area.py`, `style.css`,
`locale/bigcam.pot`, `locale/pt-BR.po`, `usr/share/locale/pt_BR/…/bigcam.mo`,
`pkgbuild/PKGBUILD`, `default.nix`, `README.md`, `tests/audit/conftest.py`.

## 11. Dependências

`libsecret` (typelib `Secret-1`) — opcional em tempo de execução (sem ela, a
senha vale só durante a sessão). Nenhuma biblioteca Python nova. Testes: a
bancada usa `gst-rtsp-server`; o teste gráfico usa `xvfb-run`.

## 12. Testes e evidências

137 testes automatizados aprovados; teste gráfico na bancada 14/14; teste
gráfico com a câmera real (descoberta, identificação, estado de senha);
gravação verificada com ffprobe/ffmpeg; reconexão medida. Detalhes e capturas
em [04 Testes](IP_CAMERA_04_TESTES.md).

## 13. Desempenho

Conversão por frame 4,2 → 0,07 ms; CPU da GUI 163% → 142,5% (renderização por
software); gravação 1,8% CPU; monitor 4,1%; reconexão 0,6 s; TTFF ~0,6 s.

## 14. Executar e instalar

```bash
# a partir do repositório
python3 usr/share/biglinux/bigcam/main.py
# testes
python3 -m pytest -q tests
# pacote (Arch/BigLinux/Manjaro)
cd pkgbuild && makepkg -si
```

### Cadastrar uma câmera IP

1. Menu ☰ → **Adicionar câmera IP…**
2. **Encontrar automaticamente** (ou digite o IP/rede) → escolha a câmera.
   Ou **Adicionar manualmente**: endereço, protocolo, portas, usuário e senha.
3. **Testar conexão** → confira a prévia e os recursos.
4. **Adicionar câmera**. Ela aparece no seletor; o painel lateral (aba
   Controles) mostra PTZ, qualidade, alertas e gravação.

Yoosee: usuário `admin`, senha definida em *app Yoosee → câmera → ⚙ →
Conexão NVR*. Câmera em outra rede: ver "Acesso remoto" no documento 02.

## 15. Pendências reais

1. **Senha NVR da câmera "Garagem"** (ação do proprietário no app Yoosee). A
   etiqueta da câmera não traz senha de fábrica (campo PWA vazio). Com ela,
   rodar `tests/ipcam/run_graphical_real.py` com `REAL_PASS` para confirmar
   vídeo, áudio, PTZ, presets, IR-cut e eventos no hardware.
2. **Acesso a partir de outra rede**: decidir entre rota de sub-rede
   Tailscale/Headscale (altera a VPN) ou túnel SSH documentado.
3. **Regressão em hardware** de webcam USB, DSLR, smartphone e câmera virtual
   (sem dispositivos livres durante os testes).
4. **Medição de CPU com renderização por GPU** (as medições de GUI foram em
   Xvfb com renderização por software).
5. Traduções para os demais idiomas (o `.pot` foi atualizado; o fluxo
   automático do projeto gera os demais).

## Segurança operacional

* A senha SSH fornecida não foi gravada em nenhum arquivo do repositório nem em
  logs; **troque-a** agora que os testes terminaram.
* Na máquina de testes ficou apenas o pacote `xorg-server-xvfb` (instalado para
  os testes gráficos; pode ser removido com `sudo pacman -Rs xorg-server-xvfb`).
  A bancada RTSP foi parada e todas as cópias/arquivos temporários removidos.
