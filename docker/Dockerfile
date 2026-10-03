# Stage 1: compile the C++ solar core (bible 9.0).
FROM docker.io/library/python:3.11-slim AS native
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY native/ native/
RUN bash native/build.sh

# Stage 2: the app image. Only the compiled library is copied over, no compiler.
FROM docker.io/library/python:3.11-slim

# A READINGS_API_KEY generated at start-up is saved in the mounted data/ folder, so it survives restarts.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUFOR_ENV_FILE=/app/data/.env
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=native /build/native/libsolarsight.so native/libsolarsight.so
# Fail the build if the library cannot be loaded, instead of silently using the Python fallback.
RUN python -c "import ctypes; ctypes.CDLL('native/libsolarsight.so'); print('native core loads')"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
