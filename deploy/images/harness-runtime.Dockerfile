# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS package-base
WORKDIR /opt/arhugula
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.lock.txt /tmp/requirements.lock.txt
COPY *.whl /wheelhouse/
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt \
    && python -m pip install --no-cache-dir --no-deps --no-index --find-links=/wheelhouse \
        harness-core \
        harness-is \
        harness-as \
        harness-cp \
        harness-od \
        harness-cxa \
        harness-runtime \
    && python -m pip check \
    && harness --help >/dev/null

FROM package-base AS self-hosted-daemon
LABEL org.opencontainers.image.title="Arhugula Harness self-hosted daemon"
ENV HARNESS_DEPLOYMENT_SURFACE=self-hosted-server
ENTRYPOINT ["harness", "daemon"]

FROM package-base AS managed-cloud-daemon
LABEL org.opencontainers.image.title="Arhugula Harness managed-cloud daemon"
ENV HARNESS_DEPLOYMENT_SURFACE=managed-cloud
ENTRYPOINT ["harness", "daemon"]

FROM package-base AS sandbox-runner
LABEL org.opencontainers.image.title="Arhugula Harness sandbox runner"
ENV HARNESS_SANDBOX_RUNNER=1
ENTRYPOINT ["python"]
CMD ["-c", "import harness_runtime; print('harness-runtime sandbox runner ready')"]
