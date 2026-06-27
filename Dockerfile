# PPT Master service image: Python (skill scripts + FastAPI) + Node (Claude Code CLI).
FROM python:3.12-slim

# System deps: Node.js (for Claude Code), pandoc + cairo for the skill's converters,
# plus a C toolchain + cairo/ffi dev headers so cairosvg/pycairo/cairocffi can build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates tzdata \
        build-essential pkg-config \
        pandoc libcairo2 libcairo2-dev libffi-dev \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (the headless agent / route A executor).
RUN npm install -g @anthropic-ai/claude-code

# Non-root user — Claude Code refuses --dangerously-skip-permissions as root.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Python dependencies: skill + service.
COPY skills/ppt-master/requirements.txt /tmp/skill-requirements.txt
COPY service/requirements-service.txt /tmp/service-requirements.txt
RUN pip install --no-cache-dir -r /tmp/skill-requirements.txt \
    && pip install --no-cache-dir -r /tmp/service-requirements.txt \
    && pip install --no-cache-dir cairosvg

# Project source.
COPY . /app

# Hand ownership to the non-root user and run as them.
RUN mkdir -p /app/projects /app/uploads && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 8000

CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000"]
