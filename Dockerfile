# For local `docker build`/testing, and for MCP directories that build from a
# committed Dockerfile. Glama (glama.ai/mcp/servers) does NOT use this file —
# its admin panel generates its own Dockerfile from form fields (base image,
# Python version, build steps, CMD) independently of what's committed here.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app/pipeline_mcp_server.py"]
