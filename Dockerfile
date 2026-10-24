FROM rust:1.90-bookworm@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS spotify
RUN apt-get update && apt-get install -y --no-install-recommends libpulse-dev pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
RUN cargo install librespot --version 0.8.0 --locked --no-default-features --features pulseaudio-backend,native-tls,with-libmdns

FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
RUN apt-get update && apt-get install -y --no-install-recommends python3 pulseaudio pulseaudio-utils shairport-sync avahi-daemon avahi-utils dbus alsa-utils ca-certificates libssl3 && rm -rf /var/lib/apt/lists/*
RUN printf "flat-volumes = no\n" > /etc/pulse/daemon.conf
COPY --from=spotify /usr/local/cargo/bin/librespot /usr/local/bin/librespot
COPY . /app
ENV PULSE_SERVER=unix:/run/jukebox/pulse.sock PYTHONUNBUFFERED=1
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/python3", "/app/jukebox.py"]
