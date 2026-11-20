# Câmeras IP — 01. Auditoria do BigCam (antes das mudanças)

Base auditada: `main` em `447bfb1` (v4.5.0). Este documento registra o estado
encontrado; as correções estão nos documentos 03 e 05.

## Como o BigCam tratava câmeras IP

| Ponto | Arquivo | Situação encontrada |
|---|---|---|
| Detecção | `core/backends/ip_backend.py` | `detect_cameras()` retornava `[]`; só cadastro manual. **Confirmado.** |
| Controles | `ip_backend.py` | `get_controls()` → `[]`, `set_control()` → `False`. Nenhum controle físico. **Confirmado.** |
| Cadastro | `ui/ip_camera_dialog.py` | Diálogo só com Nome + URL; sem descoberta, sem teste, sem autenticação separada. **Confirmado.** |
| Persistência | `utils/settings_manager.py`, `ui/window.py` | `ip_cameras = [{name, url}]` em JSON. A URL incluía `usuário:senha@`. |
| Pipeline | `ip_backend.get_gst_source()` | `rtspsrc location="…credenciais…" latency=150 ! decodebin ! videoconvert`. |
| Erros/reconexão | `core/stream_engine.py::_on_bus_message` | Qualquer erro → `stop()` + mensagem genérica. **Sem reconexão**, sem detecção de travamento. |
| Áudio | — | Ramo de áudio do RTSP ignorado (pad não ligado). Sem áudio da câmera. |

## Problemas reais (priorizados)

| # | Prioridade | Problema | Risco | Solução aplicada |
|---|---|---|---|---|
| 1 | Alta (segurança) | Senha RTSP gravada em texto puro em `~/.config/bigcam/…json` dentro da URL | Vazamento de credenciais de vigilância | Senha no Secret Service (libsecret); migração automática remove a senha do arquivo |
| 2 | Alta | Sem reconexão: queda de Wi‑Fi derrubava a câmera até ação manual | Vigilância interrompida silenciosamente | Backoff exponencial 1→30 s, watchdog de 10 s sem frames |
| 3 | Alta | Erros de autenticação viravam "Falha na conexão" genérica | Usuário sem saber o que corrigir | Classificação por sondagem RTSP própria (senha incorreta / caminho inexistente / rede) |
| 4 | Média (desempenho) | `bgr_from_bgra` fazia cópia "estridada" em NumPy: **4,2 ms por frame 720p** em todo backend appsink (IP e gPhoto2) | ~13% de um núcleo só nessa cópia a 30 fps | `cv2.cvtColor` sobre a view: **0,07 ms** (resultado idêntico, testado com e sem padding) |
| 5 | Média | Em erro de câmera IP, `_find_device_users()` executava `fuser` com a URL RTSP como "dispositivo" | Chamada inútil/confusa | `fuser` só para caminhos `/dev/*` |
| 6 | Média | Logs do GStreamer registrados sem filtro | Possível vazamento de dados sensíveis em logs | Redação (`core/ipcam/security.redact`) nas mensagens de erro/aviso |
| 7 | Baixa | Nenhum controle físico (PTZ, IR, eventos) mesmo em câmeras ONVIF | Funcionalidade ausente | Subsistema `core/ipcam` |

Observação sobre o item 6: testei se o `rtspsrc` vaza a senha da URL nas
mensagens de erro (senha marcadora em URL inválida). **Não vazou** no texto de
erro/debug; a URL com senha, porém, continuava legível na propriedade
`location` do elemento. O vazamento concreto era o arquivo de configurações.

## Outras observações (não alteradas)

* O seletor de câmeras escolhe a primeira câmera detectada ao iniciar mesmo
  com "Iniciar pré-visualização automaticamente" desligado
  (`ui/camera_selector.py`). Não afeta câmeras IP; registrado para avaliação.
* A suíte de testes existente (115 testes) não tinha nenhum teste de câmera IP
  além de validação de URL.
* Os testes de auditoria chamavam o fluxo legado de URL com senha; com o novo
  armazenamento, isso gravaria no chaveiro real do desenvolvedor. Os
  `conftest.py` agora forçam `BIGCAM_DISABLE_KEYRING=1` (corrigido; um item de
  teste criado durante o desenvolvimento foi removido do chaveiro).

## Funcionalidades preservadas

USB/V4L2, PipeWire, libcamera, gPhoto2 (DSLR), smartphone (navegador, scrcpy,
AirPlay) e câmera virtual não tiveram lógica alterada. As únicas mudanças
compartilhadas são a conversão BGRA→BGR mais rápida (`utils/frame_buffers.py`)
e o filtro de `fuser`. Ver regressão em `IP_CAMERA_04_TESTES.md`.
