# Build stage: build GPAC from source with the confirmed configure flags
# (SCOPE.md §6, §7). Pin an exact commit once validated against real
# franken-ts output -- do not float on `master`.
FROM debian:bookworm-slim AS gpac-build

# TODO(SCOPE.md §7): pin this to an exact, validated commit before
# production use. Left as a build arg so it's easy to bump deliberately.
ARG GPAC_COMMIT=master

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
        build-essential pkg-config yasm nasm \
        zlib1g-dev libpng-dev libjpeg-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone https://github.com/gpac/gpac.git gpac
WORKDIR /src/gpac
RUN git checkout "${GPAC_COMMIT}"

# IMPORTANT: do NOT pass --disable-svg -- it triggers a real upstream
# compile bug (base_scenegraph.c, unbalanced #ifndef GPAC_DISABLE_SVG
# block) that cascades into unrelated link failures (SCOPE.md §6).
#
# NOTE: as of the `master` commit validated here, GPAC's configure script
# no longer has --static-mp4box/--disable-atsc/--disable-mpegts-xevc/
# --disable-avi flags (present in an earlier snapshot referenced by
# SCOPE.md §6/§7) -- `./configure --disable-x11` alone was built and
# confirmed working end-to-end against real franken-ts output (including
# `scte35dec:mode=passthrough` and cue-driven `dasher` segmentation) in
# this environment. Re-check `./configure --help` if bumping GPAC_COMMIT.
RUN ./configure --disable-x11 \
    && make -j"$(nproc)" \
    && make install

# Collect runtime shared-library dependencies for the runtime stage.
RUN mkdir -p /out/lib /out/bin \
    && cp /usr/bin/gpac /usr/bin/MP4Box /out/bin/ \
    && ldd /usr/bin/gpac /usr/bin/MP4Box \
        | awk '{print $3}' | grep '^/' | sort -u \
        | xargs -I{} cp -L {} /out/lib/ || true

# ---------------------------------------------------------------------------

FROM debian:bookworm-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gpac-build /out/bin/ /usr/local/bin/
COPY --from=gpac-build /out/lib/ /usr/local/lib/
RUN ldconfig

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY bake.py serve.py gpac_pipeline.py scte35_signaling.py loop_math.py ./

ENTRYPOINT ["python3"]
CMD ["serve.py", "--help"]
