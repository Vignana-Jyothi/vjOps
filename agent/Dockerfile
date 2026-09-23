FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates iproute2 docker.io nginx-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY viljaops_agent ./viljaops_agent

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "viljaops_agent.main"]
