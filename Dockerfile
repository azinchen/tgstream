ARG IMAGE_VERSION=N/A
ARG BUILD_DATE=N/A

# s6 overlay builder
FROM alpine:3.24.1 AS s6-builder

ARG TARGETARCH
ARG TARGETVARIANT

ENV PACKAGE="just-containers/s6-overlay"
ENV PACKAGEVERSION="3.2.3.2"

RUN echo "**** install mandatory packages ****" && \
    apk --no-cache --no-progress add \
        tar=1.35-r5 \
        xz=5.8.3-r0 \
        && \
    echo "**** create folders ****" && \
    mkdir -p /s6 && \
    echo "**** download ${PACKAGE} ****" && \
    echo "Target arch: ${TARGETARCH}${TARGETVARIANT}" && \
    case "${TARGETARCH}${TARGETVARIANT}" in \
        amd64)      s6_arch="x86_64" ;; \
        arm64)      s6_arch="aarch64" ;; \
        armv7)      s6_arch="arm" ;; \
        *)          s6_arch="x86_64" ;; \
    esac && \
    s6_url_base="https://github.com/${PACKAGE}/releases/download/v${PACKAGEVERSION}" && \
    wget -q "${s6_url_base}/s6-overlay-noarch.tar.xz" -qO /tmp/s6-overlay-noarch.tar.xz && \
    wget -q "${s6_url_base}/s6-overlay-${s6_arch}.tar.xz" -qO /tmp/s6-overlay-binaries.tar.xz && \
    wget -q "${s6_url_base}/s6-overlay-symlinks-noarch.tar.xz" -qO /tmp/s6-overlay-symlinks-noarch.tar.xz && \
    wget -q "${s6_url_base}/s6-overlay-symlinks-arch.tar.xz" -qO /tmp/s6-overlay-symlinks-arch.tar.xz && \
    tar -C /s6/ -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    tar -C /s6/ -Jxpf /tmp/s6-overlay-binaries.tar.xz && \
    tar -C /s6/ -Jxpf /tmp/s6-overlay-symlinks-noarch.tar.xz && \
    tar -C /s6/ -Jxpf /tmp/s6-overlay-symlinks-arch.tar.xz

# rootfs builder
FROM alpine:3.24.1 AS rootfs-builder

ARG IMAGE_VERSION
ARG BUILD_DATE

COPY root/ /rootfs/
RUN chmod +x /rootfs/usr/local/bin/* || true && \
    chmod +x /rootfs/etc/s6-overlay/s6-rc.d/*/run || true && \
    chmod +x /rootfs/etc/s6-overlay/s6-rc.d/*/finish || true && \
    sed -i "s|__IMAGE_VERSION__|${IMAGE_VERSION}|g; s|__BUILD_DATE__|${BUILD_DATE}|g" \
        /rootfs/usr/local/bin/entrypoint
COPY --from=s6-builder /s6/ /rootfs/

# Main image
FROM alpine:3.24.1

ARG IMAGE_VERSION
ARG BUILD_DATE
ARG TARGETARCH

LABEL org.opencontainers.image.authors="Alexander Zinchenko <alexander@zinchenko.com>" \
      org.opencontainers.image.description="Capture Telegram channel live streams, restream to Plex/Jellyfin as live TV and record to files." \
      org.opencontainers.image.title="tgstream" \
      org.opencontainers.image.source="https://github.com/azinchen/tgstream" \
      org.opencontainers.image.url="https://github.com/azinchen/tgstream" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=120000 \
    DISPLAY=:99 \
    PULSE_RUNTIME_PATH=/run/pulse \
    PULSE_SERVER=unix:/run/pulse/native \
    PYTHONUNBUFFERED=1

RUN echo "**** install mandatory packages ****" && \
    apk --no-cache --no-progress add \
        busybox-extras=1.37.0-r31 \
        chromium=151.0.7922.108-r0 \
        curl=8.21.0-r0 \
        dbus=1.16.2-r2 \
        ffmpeg=8.1.2-r0 \
        font-dejavu=2.37-r6 \
        font-noto=2026.06.01-r0 \
        jq=1.8.1-r0 \
        libqrencode-tools=4.1.1-r3 \
        libva=2.23.0-r0 \
        libva-utils=2.23.0-r0 \
        mesa-va-gallium=26.1.6-r0 \
        pulseaudio=17.0-r7 \
        py3-websocket-client=1.9.0-r1 \
        python3=3.14.7-r1 \
        tzdata=2026c-r0 \
        x11vnc=0.9.17-r1 \
        xvfb=21.1.24-r0 \
        && \
    echo "**** install x86-only Intel VAAPI drivers ****" && \
    if [ "${TARGETARCH}" = "amd64" ]; then \
    apk --no-cache --no-progress add \
        intel-media-driver=26.2.1-r0 \
        libva-intel-driver=2.4.1-r2 \
        ; fi && \
    echo "**** cleanup ****" && \
    rm -rf /tmp/* /var/cache/apk/*

COPY --from=rootfs-builder /rootfs/ /

# /status answers in capture and guide mode; login mode is interactive and
# always reports healthy. start-period covers slate generation + browser boot.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["/usr/local/bin/tgstream-healthcheck"]

EXPOSE 8409 5900

ENTRYPOINT ["/usr/local/bin/entrypoint"]
