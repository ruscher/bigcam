# Análise de Recursos do guvcview para BigCam

Análise comparativa entre **guvcview 2.2.2** (C/GTK3) e **BigCam 4.1.0** (Python/GTK4/Adwaita).
Objetivo: identificar funcionalidades relevantes para implementar no BigCam.

---

## 1. Pré-definições de Hardware (Control Profiles)

### guvcview
- Salva/carrega todos os controles V4L2 via arquivo de texto
- Formato proprietário: `#V4L2/CTRL/0.0.2` com validação de min/max/step
- Localização: escolhida pelo usuário via diálogo de arquivo
- Não é per-camera automaticamente (o usuário gerencia os nomes)
- Valida compatibilidade: verifica ID + ranges antes de aplicar

### BigCam (já implementado ✅)
- `camera_profiles.py` com `save_profile()` / `load_profile()`
- Formato JSON, armazenamento per-camera
- Auto-save de perfil "default" ao alterar controles

### O que implementar
| Recurso | Prioridade | Descrição |
|---------|------------|-----------|
| **Perfis nomeados pelo usuário** | Alta | Permitir criar/renomear/deletar perfis além do "default" |
| **Exportar/importar perfil** | Média | Salvar/carregar perfil como arquivo JSON para compartilhar |
| **Botão "Reset to Hardware Defaults"** | Alta | Restaurar todos os controles para os valores `default` do V4L2 |
| **Perfil por resolução/formato** | Baixa | Associar perfil a uma combinação resolução+formato |

---

## 2. Controles de Imagem (V4L2 Controls)

### guvcview
Suporta 8 tipos de controles V4L2:

| Tipo | Widget | Exemplo |
|------|--------|---------|
| INTEGER | HScale + SpinButton | Brightness, Contrast, Zoom |
| BOOLEAN | CheckButton | Auto WB, Auto Focus |
| MENU | ComboBox | Auto Exposure modes |
| INTEGER_MENU | ComboBox numérico | WB presets |
| STRING | Entry + Apply | Firmware version |
| INTEGER64 | Entry + Apply | Timestamps |
| BITMASK | Hex Entry + Apply | Bit flags |
| BUTTON | Action button | Trigger Focus, Reset |

**Recursos extras do guvcview:**
- Pan/Tilt com botões direcionais (±) e seletor de step
- Autofocus contínuo via checkbox virtual (quando hardware suporta)
- Logitech LED mode (Off/On/Blinking/Auto)
- Detecção de modo Bayer com seletor de padrão
- Subscrição de eventos V4L2 para detectar mudanças externas
- Desativação automática de controles dependentes (ex: auto-exposure desativa exposure manual)

### BigCam (já implementado ✅)
- 4 tipos: Boolean (SwitchRow), Menu (ComboRow), Integer (Scale), String (EntryRow)
- 7 categorias com ícones: Image, Exposure, Focus, WB, Capture, Status, Advanced
- Debounce de 50ms nos sliders
- Reset por grupo
- Controles de software: zoom, sharpness, backlight, pan/tilt

### O que implementar
| Recurso | Prioridade | Descrição |
|---------|------------|-----------|
| **SpinButton junto ao slider** | Alta | Campo numérico editável ao lado de cada slider (valor preciso) |
| **Controle BUTTON** | Média | Suportar tipo BUTTON (ex: "Trigger Autofocus") |
| **Dependências de controles** | Alta | Auto-greyout quando controle pai está em modo auto |
| **Subscrição de eventos V4L2** | Baixa | Detectar mudanças feitas por apps externos |
| **Pan/Tilt com botões ±** | Média | Botões incrementais para pan/tilt hardware |
| **Autofocus Software (DCT)** | Baixa | Foco por software usando DCT em blocos (para cams sem AF) |

---

## 3. Codecs de Vídeo

### guvcview — 13 codecs suportados via FFmpeg/libavcodec

| Codec | Encoder | Bitrate padrão | Container |
|-------|---------|----------------|-----------|
| Raw (pass-through) | N/A | N/A | AVI, MKV |
| MJPEG | mjpeg | Variable | AVI, MKV |
| MPEG-1 | mpeg1video | 3 Mbps | AVI, MKV |
| FLV1 | flv | 3 Mbps | AVI |
| WMV1 | wmv1 | 3 Mbps | AVI |
| MPEG-2 | mpeg2video | 3 Mbps | AVI, MKV |
| MS MPEG-4 V3 | msmpeg4v3 | 3 Mbps | AVI, MKV |
| MPEG-4 ASP | mpeg4 | 1.5 Mbps | AVI, MKV |
| **H.264** | libx264 | 1.5 Mbps | MKV |
| **H.265** | libx265 | 1.5 Mbps | MKV |
| **VP8** | libvpx | 600 Kbps | WebM |
| **VP9** | libvpx-vp9 | 600 Kbps | WebM |
| Theora | libtheora | 1.5 Mbps | MKV |

### BigCam (atual)
- Usa GStreamer com appsrc para gravar frames processados
- Auto-detecção de encoder por hardware (NVIDIA NVENC, Intel QSV, AMD AMF)
- Fallback para x264enc (software)
- Container: MKV

### O que implementar
| Recurso | Prioridade | Descrição |
|---------|------------|-----------|
| **Seletor de codec de vídeo** | Alta | ComboBox na aba de gravação: H.264, H.265, VP9, MJPEG |
| **Controle de qualidade/bitrate** | Alta | Slider ou SpinButton para CRF/bitrate |
| **Pass-through MJPEG** | Média | Salvar MJPEG nativo sem recodificação (economia de CPU) |
| **Seletor de container** | Média | MKV vs WebM vs MP4 |
| **Preferência HW vs SW** | Baixa | Opção para forçar encoder software ou hardware |

---

## 4. Codecs de Áudio

### guvcview — 6 codecs suportados

| Codec | Encoder | Bitrate padrão | Uso |
|-------|---------|----------------|-----|
| PCM Float 32 | pcm_f32le | ~1.4 Mbps | Sem perdas |
| MP2 | mp2 | 160 Kbps | Legado |
| **MP3** | libmp3lame | 160 Kbps | Universal |
| **AC-3** | ac3 | 160 Kbps | Surround |
| **AAC** | aacenc | 64 Kbps | Moderno/compacto |
| **Vorbis** | libvorbis | 64 Kbps | WebM |

### BigCam (atual)
- Captura áudio via PipeWire/PulseAudio na gravação de vídeo
- Codec fixo (determinado pelo pipeline GStreamer)

### O que implementar
| Recurso | Prioridade | Descrição |
|---------|------------|-----------|
| **Seletor de codec de áudio** | Alta | ComboBox: AAC, MP3, Vorbis, PCM |
| **Controle de bitrate de áudio** | Média | 64-320 Kbps |
| **Seletor de dispositivo de áudio** | Alta | Dropdown com microfones PipeWire disponíveis |
| **Indicador de nível (VU meter)** | Média | Barra animada mostrando intensidade do áudio |

---

## 5. Recursos de Gravação

### guvcview
- Ring buffer de vídeo (~1.5s) para absorver picos de I/O
- Timestamping monotônico (corrige timestamps USB unreliable)
- Extração de H.264 SPS/PPS para codec private data
- Thread de encoding separada da captura
- Detecção de overflow/underflow no áudio com compensação

### BigCam (atual)
- `VideoRecorder` com GStreamer appsrc
- Detecção automática de encoder por hardware GPU
- Timer de gravação na UI
- Gravação com efeitos aplicados

### O que implementar
| Recurso | Prioridade | Descrição |
|---------|------------|-----------|
| **Página de configuração de gravação** | Alta | Seção dedicada com codec, qualidade, formato |
| **Pass-through recording** | Média | Gravação direta sem processar (economia de CPU) |
| **Limite de tamanho/duração** | Baixa | Auto-parar após X minutos ou Y MB |

---

## 6. Efeitos de Áudio (guvcview)

| Efeito | Descrição |
|--------|-----------|
| Echo | Delay com feedback |
| Reverb | 4 filtros comb paralelos |
| Wahwah | Modulação LFO por fase |
| Fuzz | Distorção/saturação |
| Ducky | Compressão sidechain |

> **Nota:** BigCam não tem efeitos de áudio. Implementar com baixa prioridade — foco nos efeitos de vídeo que já existem.

---

## 7. Resumo de Prioridades

### 🔴 Alta Prioridade
1. Seletor de codec de vídeo (H.264/H.265/VP9/MJPEG)
2. Seletor de codec de áudio (AAC/MP3/Vorbis)
3. Seletor de dispositivo de áudio (microfone)
4. Controle de qualidade/bitrate de vídeo
5. SpinButton junto aos sliders de controle V4L2
6. Auto-desativação de controles dependentes (auto-exposure → exposure)
7. Perfis de câmera nomeados pelo usuário
8. Botão "Reset to Hardware Defaults"

### 🟡 Média Prioridade
9. Seletor de container (MKV/WebM/MP4)
10. Controle de bitrate de áudio
11. Indicador VU meter para áudio
12. Pass-through MJPEG recording
13. Controle BUTTON V4L2 (Trigger Autofocus)
14. Pan/Tilt com botões incrementais
15. Exportar/importar perfil de câmera

### 🟢 Baixa Prioridade
16. Subscrição de eventos V4L2
17. Autofocus software via DCT
18. Perfil por resolução/formato
19. Efeitos de áudio (echo, reverb)
20. Limite de tamanho/duração na gravação
21. Preferência encoder HW vs SW

---

## 8. Arquitetura Proposta para Implementação

### Onde adicionar no BigCam

```
ui/
├── camera_controls_page.py  ← SpinButtons, dependências, BUTTON type
├── settings_page.py         ← Seletor áudio, perfis nomeados
├── recording_settings.py    ← NOVO: codec vídeo/áudio, bitrate, container
└── window.py                ← VU meter na barra, novo tab/botão recording settings

core/
├── camera_profiles.py       ← Perfis nomeados, import/export
├── backends/v4l2_backend.py ← Eventos V4L2, dependências de controles
└── video_recorder.py        ← Múltiplos codecs, pass-through, containers
```

### Approach GStreamer para Codecs
BigCam já usa GStreamer — implementar codecs via elementos GStreamer é mais idiomático que chamar FFmpeg diretamente:

| Codec | Elemento GStreamer |
|-------|-------------------|
| H.264 (HW) | nvh264enc / vaapih264enc / qsvh264enc |
| H.264 (SW) | x264enc |
| H.265 (HW) | nvh265enc / vaapih265enc |
| H.265 (SW) | x265enc |
| VP9 | vp9enc |
| MJPEG | jpegenc |
| AAC | fdkaacenc / avenc_aac |
| MP3 | lamemp3enc |
| Vorbis | vorbisenc |
| Opus | opusenc |
| MKV | matroskamux |
| WebM | webmmux |
| MP4 | mp4mux |
