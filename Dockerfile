# The README tells you to run validation in an isolated container. Here is one.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user: validation executes the target repo's test suite.
RUN useradd --create-home --shell /usr/sbin/nologin agent
WORKDIR /app

COPY pyproject.toml README.md ./
COPY agent ./agent
RUN pip install --no-cache-dir -e .

ENV AGENT_WORKDIR=/tmp/comment-pr-agent \
    PYTHONUNBUFFERED=1
USER agent

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz').read()" || exit 1

CMD ["uvicorn", "agent.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
