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

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
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
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
