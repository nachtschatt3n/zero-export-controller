FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
      httpx==0.27.2 \
      prometheus-client==0.20.0

COPY controller.py .

EXPOSE 8080

USER 10001:10001

CMD ["python", "-u", "controller.py"]
