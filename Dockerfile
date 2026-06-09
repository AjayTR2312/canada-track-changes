FROM public.ecr.aws/docker/library/python:3.12-slim

RUN apt-get update && apt-get install -y \
    build-essential curl poppler-utils \
    libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1234 deploymentgroup && \
    useradd -m -u 1234 -g deploymentgroup deployment

WORKDIR /workspace

RUN mkdir -p /workspace/pipeline_jobs /workspace/pipeline_output /workspace/logs && \
    chown -R deployment:deploymentgroup /workspace

COPY --chown=deployment:deploymentgroup requirements.txt /workspace/requirements.txt

USER deployment
ENV PATH="/home/deployment/.local/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

USER root
COPY --chown=deployment:deploymentgroup . /workspace
USER deployment

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=80s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1
