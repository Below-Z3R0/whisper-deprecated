# whisper-deprecated

Snapshot histórico del **stack de transcripción local** que vivía dentro de `Knowledge/.rag/`. Se
retiró cuando el RAG en Python se archivó; la transcripción pasa a Sonus.

Se conserva como referencia, no como proyecto mantenido.

## Qué hacía

Transcribía video a Markdown y SRT **100% local**, sin APIs externas, con
`faster-whisper large-v3-turbo`.

```
cli/video-transcribe              capa 1: CLI (transcribe, batch, status, start/stop-server)
mcp_server/whisper_mcp_server.py  capa 2: MCP JSON-RPC sobre stdio (5 tools)
whisper/whisper_server.py         capa 3: daemon, faster-whisper + ffmpeg, socket Unix
systemd/whisper-server.service    capa 4: unidad de usuario (disabled al archivarse)
docs/whisper-system.md            arquitectura y operación, tal como estaba documentada
```

**Contrato entre capas:** socket Unix en `/tmp/whisper-server.sock`, mensajes JSON por línea.

| Tool MCP | Qué hace |
|---|---|
| `transcribe` | encola un video; devuelve resultado o `{status: "still_running", job_id}` |
| `transcribe_status` | un poll, siempre rápido |
| `transcribe_batch` | un job por video de un directorio |
| `list_models` | modelos disponibles |
| `get_status` | estado del server |

El trabajo vive en el server, no en la llamada: cerrar la terminal no pierde la transcripción. Ese
diseño vino de un bug concreto — el `transcribe` viejo bloqueaba el socket durante toda la
transcripción y Hermes mataba la llamada a los 300 s, perdiendo el trabajo.

## Requisitos que tenía

- `faster-whisper` en el intérprete de Python (quedó en el Python del sistema, no en el venv).
- `ffmpeg` para extraer audio 16 kHz mono.
- El modelo lo descarga `faster-whisper` a la cache de HuggingFace; no va en el repo.

## Por qué se archivó

El código vivía dentro de `Knowledge/.rag/`, que se retiró del vault. La transcripción se va a
reconectar por Sonus (Auris), que ya expone ASR sobre ONNX y comparte el catálogo de modelos con
Vox.

Mientras tanto, el MCP `whisper` se quitó de todos los perfiles de Hermes: apuntaba a un archivo
que ya no existe.

## Qué NO está acá

- El venv (444 MB) y los modelos descargados.
- La unidad de systemd instalada: quedó archivada en `systemd/`, pero se desinstaló del sistema.
- El CLI instalado en `~/.local/bin/video-transcribe`: también se retiró.

## Instalación original

```bash
cp cli/video-transcribe ~/.local/bin/ && chmod +x ~/.local/bin/video-transcribe
cp systemd/whisper-server.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now whisper-server.service
```

Las rutas del CLI y de la unidad apuntaban a `~/Documents/Knowledge/.rag/`; para reutilizarlo hay que
reapuntarlas.

## Licencia

Sin licencia explícita en el original. Al publicarse como archivo histórico no se otorga ningún
permiso adicional sobre el código.
