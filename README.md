# comfy-imgpool

Two small, dependency-free CLIs for running ComfyUI image generation from scripts and LLM agents:

- **`imggen`** — one command to generate an image, with sane defaults, memory/VRAM guards and a single-line JSON result.
- **`imgpool`** — a local task queue in front of `imggen` for multiple concurrent callers, with per-job ids, queue introspection, previous/next context and two-layer logs.

Both are plain Python 3 (standard library only), bind to `127.0.0.1`, and are designed to be called by agents: stdout is always one JSON object, progress goes to stderr.

This is a **reference implementation** extracted from a working AMD RX 6900 XT (gfx1030) + ROCm setup. The model names and measured timings are specific to that machine; see [Adapting](#adapting) before using it elsewhere.

## Architecture

```
agent / script
      │  submit (HTTP)
      ▼
  imgpool  ──────────────►  imggen  ──────────►  ComfyUI /prompt
  (127.0.0.1:8199)           (one GPU job        (127.0.0.1:8188)
      │                       at a time)
      │
      ├─ queue / status / result / logs by job id
      ├─ prev/next context (model + source + state)
      └─ idle watchdog: stop ComfyUI after N idle seconds
```

`imgpool` serializes GPU work (one job at a time by default) while accepting concurrent submissions from any number of callers. `imggen` also holds its own file lock, so direct CLI calls are safe too.

## Requirements

- Linux, Python 3.9+
- A running ComfyUI with the HTTP API enabled
- The models referenced in `imggen`'s `MODELS` table (edit it for your own files)
- Optional: `systemd --user` for autostart and idle shutdown
- Optional: the custom ComfyUI nodes used by the reference config (`RDNA2Attention`, `ForceUnloadModels`) and quantized `convrot` models for the AMD acceleration path

`imggen` expects a `comfy-ensure.sh` helper to start a stopped ComfyUI (see `examples/comfy-ensure.sh`). If you always keep ComfyUI running, set `COMFY_ENSURE` to any no-op script, or start ComfyUI yourself before calling `imggen`.

## Install

```bash
git clone https://github.com/djasdh/comfy-imgpool.git
cd comfy-imgpool
chmod +x bin/imgpool bin/imggen

export COMFY_ROOT=/path/to/dir-containing-ComfyUI   # e.g. ~/AI_gen_video_img
export COMFY_URL=http://127.0.0.1:8188
export COMFY_UNIT=comfyui                           # systemd --user unit name
export PATH="$PWD/bin:$PATH"
```

`COMFY_ROOT` can often be omitted: both tools search upward from the current directory and from the script location for a directory that contains `ComfyUI/`.

## Quick start

### Generate one image

```bash
imggen --list                        # JSON capability list: models, presets, timings
imggen "a red apple on a wooden table"
imggen -m anima "anime girl in a sunflower field"
imggen -m krea2 --preset 2k "cinematic swordswoman"
imggen -m anima --size 1344x768 "wide cyberpunk street"
imggen -m anima --lora modern-anime-lora:0.8 "portrait"
imggen -m moss --lora ba "anime-style street at night"   # alias -> 0.8
imggen -m moss --variant sft-int4 "a green apple"        # swap the weight file
imggen --edit input.png "turn the sword into a glowing blue blade"   # MOSS edit
imggen --dry -m krea2 "x"            # print the API graph without submitting
```

### Queue many images

```bash
imgpool start
imgpool submit -m moss "a green apple" --source agent-a      # waits for result
imgpool submit -m anima --no-wait "a blue cup" --source agent-b
imgpool status                                               # current queue
imgpool status --id job_20261120_004606_abc123                # one job + prev/next
imgpool result --id job_...                                  # result + prev/next
imgpool wait --id job_...
imgpool list --limit 20
imgpool logs --id job_... --tail 50                          # this job's imggen output
imgpool logs --pool                                          # pool-level events
imgpool cancel --id job_... --source agent-a
imgpool stop                                                 # refuses if jobs are running
```

## JSON contract

Success (`imggen`):

```json
{
  "ok": true,
  "model": "anima",
  "preset": "fast",
  "width": 1024,
  "height": 1024,
  "output_width": 1536,
  "output_height": 1536,
  "seed": 424242,
  "images": ["/abs/path/ComfyUI/output/imggen_anima_...png"],
  "seconds": 13.0,
  "cold_likely": false,
  "restarted": false,
  "warnings": []
}
```

Failure is still one JSON object, with a machine-usable `error` and a human `hint`:

```json
{"ok": false, "error": "risky_preset",
 "message": "moss/1536 unverified", "hint": "add --allow-risky"}
```

Exit codes:

| code | meaning |
|---|---|
| 0 | success |
| 2 | bad arguments / validation |
| 3 | service unavailable |
| 4 | memory or VRAM guard refused |
| 5 | ComfyUI rejected the graph (includes `node_errors`) |
| 6 | execution failed or timed out |

`imgpool` results add `prev` / `next` and a `queue` snapshot:

```json
{
  "ok": true,
  "id": "job_20261120_004606_abc123",
  "state": "done",
  "model": "moss",
  "source": "agent-a",
  "seconds": 9.1,
  "result": { "...": "the imggen JSON above" },
  "prev": {"model": "anima", "source": "agent-b", "state": "done",
           "image": "/abs/path/...png"},
  "next": {"model": "krea2", "source": "agent-c", "state": "queued"},
  "queue": {"running": [], "pending": [], "counts": {"done": 10}}
}
```

## Logs

Two layers, so you can tell whether a failure is the pool's or the CLI's:

| file | contents |
|---|---|
| `logs/imgpool/pool.log` | pool events: submit, start (argv), finish (exit code, seconds, error), cancel, timeout, idle stop |
| `logs/imgpool/cli/<job_id>.log` | raw `imggen` output for that job, streamed while it runs |
| `logs/imgpool/jobs.jsonl` | structured records, restored into memory on restart (last 200) |

Rule of thumb:

- `spawn_failed` / `timeout` / `bad_json` in `pool.log` with an almost-empty CLI log → pool-layer problem
- `finish exit_code=2|6` plus `{"ok":false,"error":...}` in the CLI log → `imggen` or argument problem

## Remote access and auth

By default `imgpool` listens on `127.0.0.1` with no auth. To let another machine
drive it (for example an agent on a different host):

```bash
export IMGPOOL_BIND=10.0.0.5          # bind one interface
export IMGPOOL_TOKEN=$(openssl rand -hex 24)
imgpool serve --port 8199 --idle-stop-seconds 900
printf '%s\n' "$IMGPOOL_TOKEN" > ~/.config/comfy-imgpool.token
chmod 600 ~/.config/comfy-imgpool.token
```

- When `IMGPOOL_TOKEN` is set, every request must send `X-Imgpool-Token`
  (or `?token=`). Requests without it get `401`.
- `IMGPOOL_BIND` may be a specific interface; the server also keeps a
  `127.0.0.1` listener so local clients work without extra env.
- Local clients read the token from `~/.config/comfy-imgpool.token` when
  `IMGPOOL_TOKEN` is not exported.
- Download a generated image with `GET /image?id=<job_id>&index=0`
  (token required). Job JSON also exposes `image_urls`.
- There is still no per-user authorization; treat the token as a shared secret
  and keep the port on a private network.

## Hermes agent plugin

`examples/hermes-plugin/imgpool/` is a ready-made Hermes image-generation
provider that drives this pool over HTTP. Install it as a user plugin:

```bash
cp -r examples/hermes-plugin/imgpool ~/.hermes/plugins/image_gen/
hermes plugins enable imgpool
```

Then point `~/.hermes/config.yaml` at the pool:

```yaml
image_gen:
  provider: imgpool
  model: imgpool-moss
  imgpool:
    base_url: http://10.0.0.5:8199
    token: <IMGPOOL_TOKEN>
    timeout: 600
    poll_interval: 2
```

Model ids map to `imggen` presets: `imgpool-moss`, `imgpool-anima-fast`,
`imgpool-anima-1536`, `imgpool-krea2-720p`, `imgpool-krea2-2k`.

## Idle shutdown

`imgpool` stops ComfyUI after **900 seconds** (15 min) with no generation requests:

- no queued/running pool jobs
- ComfyUI's own `/queue` is empty
- no activity heartbeat within the timeout

A ComfyUI that comes up from *any* source resets the idle timer (down→up detection), and `imggen` refreshes the heartbeat while it runs. The next request starts ComfyUI again automatically.

```bash
imgpool serve --idle-stop-seconds 900   # 0 disables
```

## Autostart (systemd user unit)

See `examples/imgpool.service`:

```bash
mkdir -p ~/.config/systemd/user
cp examples/imgpool.service ~/.config/systemd/user/
# edit COMFY_ROOT / COMFY_UNIT / paths
systemctl --user daemon-reload
systemctl --user enable --now imgpool.service
loginctl enable-linger "$USER"     # start without an active login session
```

## Configuration

| env var | default | used by |
|---|---|---|
| `COMFY_ROOT` | auto-detect | both — dir containing `ComfyUI/` |
| `COMFY_DIR` | `$COMFY_ROOT/ComfyUI` | `imggen` |
| `COMFY_URL` | `http://127.0.0.1:8188` | both |
| `COMFY_UNIT` | `comfyui` | `imgpool` idle stop |
| `COMFY_ENSURE` | `$COMFY_ROOT/bin/comfy-ensure.sh` | `imggen` |
| `COMFY_LOG` | `$COMFY_ROOT/logs/comfy-2k.log` | `imggen` |
| `IMGGEN_BIN` | `imggen` next to `imgpool` | `imgpool` |
| `IMGPOOL_PORT` | `8199` | `imgpool` |
| `IMGPOOL_BIND` | `127.0.0.1` | `imgpool` server bind address |
| `IMGPOOL_TOKEN` | unset | `imgpool` shared token (enables auth) |
| `IMGPOOL_TOKEN_FILE` | `~/.config/comfy-imgpool.token` | `imgpool` client token fallback |
| `IMGPOOL_URL` | derived from bind/port | `imgpool` client base URL |
| `COMFY_DIR` | `$COMFY_ROOT/ComfyUI` | `/image` path validation |
| `IMGPOOL_CONCURRENCY` | `1` | `imgpool` |
| `IMGPOOL_IDLE_STOP_SECONDS` | `900` | `imgpool` |
| `IMGPOOL_LOG_DIR` | `$COMFY_ROOT/logs/imgpool` | `imgpool` |
| `IMGPOOL_STATE_DIR` | `$TMPDIR/comfy-imgpool` | both (locks, pid, heartbeat) |
| `IMGPOOL_ACTIVITY_FILE` | `$IMGPOOL_STATE_DIR/comfy-last-activity` | both |
| `IMGPOOL_QUIET` | unset | `imgpool` (suppress stderr mirror under systemd) |

## Adapting

The `MODELS` table near the top of `bin/imggen` is the only place that knows about model files. Each entry defines:

- `unet`, `clip` (name + loader type), `vae`
- accelerator flags for the `RDNA2Attention` node
- memory thresholds (`min_avail_gb`, `vram_need_gb`)
- `default_lora` / `default_lora_max_steps`: a LoRA mounted automatically while
  `steps` stays at or below that value (MOSS uses this for its 4-step distilled
  checkpoint; without it, `steps=4 cfg=1` only produces blur)
- `default_variant` / `variants`: interchangeable weight files for the same
  graph. Each variant is a sparse override of the model entry (`unet`, `accel`,
  memory thresholds, `notes`, ...). Select it with `--variant NAME` or the
  `IMGPOOL_VARIANT` env var; `--list` reports every variant. MOSS ships
  `sft-int8` (default), `sft-int4` and `sft-fp16`.
- presets: resolution, steps, sampler, optional two-pass upscale

If your ComfyUI does not have `RDNA2Attention` / `ForceUnloadModels`, remove those nodes from `build_graph()` (or keep a compatible node in your install). The measured timings in this repo come from a 16 GB AMD RX 6900 XT; they are reference numbers, not guarantees.

Presets are deliberately small and explicit rather than clever: a preset name maps to a fixed graph. Add or edit presets for your own models.

`LORA_ALIASES` next to `DEFAULT_PRESET` maps a short name to `(file,
default_strength)`, so `--lora ba` expands to `ba_style_moss_v09:0.8`.
Edit it to match the LoRAs on your machine.

## Safety

- Both tools bind only to `127.0.0.1` by default. `IMGPOOL_BIND` can expose one
  interface; when you do that, set `IMGPOOL_TOKEN` (see
  [Remote access and auth](#remote-access-and-auth)). Never expose the port to
  the public internet.
- `imgpool cancel` checks `--source` for ownership; crossing sources requires `--force`.
- `imgpool stop` refuses to stop while jobs are running unless `--force`.
- Memory guards can restart ComfyUI when host RAM or VRAM is too low; `--no-restart` disables that.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with ComfyUI.
