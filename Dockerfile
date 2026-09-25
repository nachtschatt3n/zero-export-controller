FROM python:3.12-slim

WORKDIR /app

# Pull Debian security fixes at build time instead of waiting for the next
# upstream python:3.12-slim rebuild; the base tag alone lags the archive.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      httpx==0.27.2 \
      prometheus-client==0.20.0

COPY controller.py .

EXPOSE 8080

USER 10001:10001

CMD ["python", "-u", "controller.py"]
