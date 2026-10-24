# Admiral Office Jukebox

An ARM64 Linux audio workload for Admiral, with a local Admiral-branded dashboard. People use the AirPlay menu or Spotify's device picker; there is no extra client app. This package is a workload, not an Admiral OS image.

## What is implemented

- Classic AirPlay audio via Shairport Sync 3.3.8, and Spotify Connect via librespot 0.8.0. Spotify Premium is required. AirPlay 2, Chromecast, Bluetooth and multiroom synchronisation are not included.
- A local, responsive dashboard: source stream status, receiver listening checks, audio health, recovery controls, bounded activity history and authenticated diagnostic downloads.
- Web-selectable analogue 3.5 mm, USB and HDMI ALSA outputs. Only outputs exposed by the device's Linux driver appear. Names are stored by ALSA card ID rather than numeric card order. Choose the explicitly labelled analogue/headphone endpoint rather than System default for the office installation.
- A changed output is tried before saving. If it cannot open within 15 seconds, the previous settings are restored. The previous settings stay on disk during the trial, so a power failure also returns to them. An output opening does not prove the cable, amplifier or speakers are audible.
- A shared PulseAudio output with native role ducking: AirPlay has priority, Spotify is attenuated to zero while the AirPlay stream is open. A one-second reconciliation loop also enforces source muting. Disconnect AirPlay to return to Spotify. Pausing an app can leave its AirPlay stream open.
- Process recovery with exponential backoff, fatal escalation after repeated service failures, audio failure escalation and a separate supervisor watchdog. PID 1 exits on persistent failures so Admiral can restart or roll back the workload.
- Atomic, synchronised settings writes on `/data`, bounded in-memory events, disabled Spotify audio caching and graceful child-process shutdown.
- An administrator key for settings, recovery and diagnostics; same-origin browser mutation checks. The key is kept only in page memory. Listening and ordinary status viewing do not need a key.
- All web fonts, logos and textures are bundled from the provided Admiral brand folder. No external fonts, scripts, analytics or image services.

The dashboard shows protocol/source streams, not the identity of every nearby phone or an invented queue. Track selection, playback and sender volume remain in the sender's app. Spotify may continue progressing while muted by AirPlay.

## Build

On an ARM64 Docker host:

```sh
docker build -t admiral-jukebox:0.1.0 .
```

For a registry build on another architecture:

```sh
docker buildx build --platform linux/arm64 -t ghcr.io/admrlos/workload-jukebox:0.1.0 --push .
```

The GitHub workflow builds and tests on a native ARM64 runner, then publishes `ghcr.io/admrlos/workload-jukebox:latest` and an immutable Git commit tag. Use the commit tag or registry digest for Admiral deployments. Package access follows the private GitHub repository; configure registry credentials in Admiral as needed. The manual build command above is an alternative versioned release. Base image manifests and the librespot version are pinned. Debian packages receive the versions available when the image is built; deploy the resulting immutable image digest and retain it for rollback. ARM64 is the tested architecture; 32-bit ARM has not been validated.

## Configure in Admiral

Create a workload configuration in the Admiral dashboard:

| Setting | Value |
| --- | --- |
| Image | Your built image, preferably by immutable registry digest |
| Command | Leave empty to retain the image's absolute entrypoint |
| Desired state | Running |
| Interactive mode | Off |
| Host network | On; necessary for local multicast discovery |
| Device isolation | Off, so the workload can access `/dev/snd` |
| Expose host D-Bus | Off; this image runs its own private system bus |
| Custom persistent volume | Mount read/write at `/data` |
| Environment `ADMIN_TOKEN` | A random secret of at least 20 characters |
| Environment `JUKEBOX_NAME` | `Admiral Office` initially; subsequently editable in the web UI |
| Environment `ALSA_DEVICE` | Optional initial output, default `default`; the saved web setting takes precedence |
| Environment `PORT` | Optional, default `8080` |

Keep the workload root filesystem writable for `/run` and service state. Admiral supplies `/dev` by default when device isolation is disabled. The device must have the appropriate kernel audio driver; a container cannot create a missing hardware driver or enable a board's physical audio port.

On first boot, visit `http://DEVICE_IP:8080`, open Settings, enter the administrator key, and choose the analogue/headphones output. Save, wait for the output check and listen with a phone. Outputs rescan every ten seconds; reconnecting a USB interface does not require refreshing the page. Identical USB cards can have ambiguous IDs: verify the label and keep the intended interface installed.

Use Ethernet for the appliance where possible. Phones must be on the same LAN, or on an intentionally configured multicast-reflected network, with Wi-Fi client isolation disabled between the clients and jukebox. On a segmented network ask IT to permit:

| Traffic | Purpose |
| --- | --- |
| UDP 5353 multicast | AirPlay and Spotify discovery |
| TCP 5000, UDP 6001–6010 | Classic AirPlay audio/control |
| TCP 4070 | Spotify Connect discovery/pairing listener |
| TCP 8080 | Local dashboard |
| Outbound DNS and HTTPS | Spotify service access |

No router port forwarding is required. Keep the control interface on the trusted office network; it is HTTP by default. Use a TLS reverse proxy if administrator access must cross an untrusted network. Keep `/data` private because librespot may cache Spotify session credentials there. Diagnostic downloads omit those credentials and raw protocol logs.

Admiral does **not** execute Docker HEALTHCHECK instructions. This workload therefore supervises itself and exits PID 1 when persistent failures require an Admiral restart. Configure a staged rollout and retain a known-good image. Admiral's documented probation/rollback behaviour applies to process failures; it cannot detect an inaudible speaker or a discovery failure on a phone.

## Run on a Linux development device without Admiral

With `/dev/snd` present:

```sh
export ADMIN_TOKEN="$(openssl rand -hex 24)"
docker compose up --build -d
```

The supplied Compose file uses host networking, mounts the audio devices and persists `/data`. It is intended for Linux. Docker Desktop's VM does not expose the Mac's analogue audio device; use the UI preview or the silent container integration test on a Mac.

## Local interface preview

```sh
python3 jukebox.py --demo --data ./state --port 8090
```

Open `http://127.0.0.1:8090`. The preview is explicitly marked as having no hardware. `/readyz` returns 503. To exercise preview settings, start with a temporary `ADMIN_TOKEN` environment variable and enter that same key in the page. No receivers run in preview mode.

## Verification

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
docker run --rm --entrypoint /usr/bin/python3 admiral-jukebox:0.1.0 /app/tests/container_smoke.py
```

The container smoke test starts the actual ARM64 receiver binaries and private D-Bus/Avahi/PulseAudio stack. Its output is deliberately a null sink. It verifies listening ports, killed-process recovery, source handover, settings rename, audio-engine restart, automatic fallback from a missing output and shutdown. It does not authenticate to Spotify, send an AirPlay session from a phone or produce audible sound.

`GET /healthz` checks the supervisor heartbeat. `GET /readyz` also checks audio availability, service processes and local receiver listening ports. Neither endpoint is an end-to-end phone or speaker test. Public `GET /api/status` powers the page. Mutations require `Authorization: Bearer ADMIN_TOKEN`.

## Required office acceptance test

Before treating this as the office's dependable jukebox:

1. Select and listen to the actual analogue 3.5 mm output. Check amplifier power and the device's hardware mixer mute/routing if the output opens but is silent.
2. Connect from the office's iPhone/Mac AirPlay menu and Spotify on both iOS and Android. Repeat disconnect/reconnect 20 times per client. Check discovery after the phone sleeps and rejoins Wi-Fi.
3. Play each protocol for at least eight hours. Listen for dropouts and inspect restart counts. Test internet loss separately: Spotify needs internet; local AirPlay should remain available.
4. Switch between the sources repeatedly. Confirm AirPlay priority and Spotify returning after AirPlay disconnects. Also test two phones trying the same protocol.
5. Kill each receiver, interrupt the network and reboot the device. Confirm the speaker returns to discovery and settings survive. For an external USB analogue interface, unplug/replug it and check recovery. A driver or amplifier fault may still require intervention.
6. Choose an unavailable output using a controlled test and confirm fallback. Verify physical cable removal is understood as distinct from an ALSA output failing to open.
7. Power-cycle and perform an Admiral image update with `/data` mounted. Confirm saved output/name/volume survive, and verify rollback on a deliberately failing canary release.

These hardware/network checks remain outstanding because no office device or audio interface has been enrolled or connected yet. No reliability claim should treat the null-sink tests as a replacement.

## Sources

- [Admiral launch, health, volumes and hardware contract](https://docs.admrl.co/core-concepts/configurations/)
- [Shairport Sync](https://github.com/mikebrady/shairport-sync)
- [librespot](https://github.com/librespot-org/librespot)
- [librespot command-line options](https://github.com/librespot-org/librespot/wiki/Options)

Third-party receivers retain their upstream licences. Their binaries and Debian licence metadata are inside the image. Admiral brand assets are supplied by the user and have no additional redistribution permission implied by this package.
