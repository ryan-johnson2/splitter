# Self-contained build: everything needed is in this repository.
#   docker build -t splitter .
# Optional: drop a private velocidrone_tracks-*.whl into ./wheels before building
# to enable the online track picker (release images are built that way). It is
# Cython-compiled in the builder stage so the image carries no readable source
# for it.
FROM python:3.12-slim AS builder
# The build stamp (scripts/stamp.py): pass --build-arg SPLITTER_BUILD=$(python scripts/stamp.py).
# The image context has no .git, so an empty arg means "pyproject version".
ARG SPLITTER_BUILD=""
WORKDIR /app
COPY libs/velocidrone-ws /app/libs/velocidrone-ws
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY scripts/stamp.py /app/scripts/stamp.py
COPY desktop/sidecar/protect.py /app/protect.py
COPY wheels* /app/wheels/
RUN python /app/scripts/stamp.py --write --stamp "$SPLITTER_BUILD" \
    && pip install --no-cache-dir /app/libs/velocidrone-ws /app \
    && if ls /app/wheels/velocidrone_tracks-*.whl >/dev/null 2>&1; then \
         apt-get update -qq && apt-get install -y -qq --no-install-recommends gcc libc6-dev >/dev/null \
         && pip install --no-cache-dir /app/wheels/velocidrone_tracks-*.whl \
         && python /app/protect.py velocidrone_tracks \
         && pip uninstall -y -q cython setuptools; \
       else echo "no velocidrone-tracks wheel: online track picker disabled"; fi

FROM python:3.12-slim
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
VOLUME /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/splitter.db
EXPOSE 8100
CMD ["python", "-m", "splitter"]
