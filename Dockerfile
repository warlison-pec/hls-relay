FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY relay.py /app/relay.py
USER 65534:65534
EXPOSE 8099
# Checks only that the relay HTTP server answers (not the origin state),
# so an origin outage never makes Docker report the relay as broken.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8099'), timeout=4).read()"]
CMD ["python", "-u", "/app/relay.py"]
