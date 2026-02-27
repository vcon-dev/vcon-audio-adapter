FROM python:3.11-slim

# Build arguments for version information (injected by CI/CD)
ARG VCON_AUDIO_ADAPTER_VERSION=dev
ARG VCON_AUDIO_ADAPTER_GIT_COMMIT=unknown
ARG VCON_AUDIO_ADAPTER_BUILD_TIME=unknown

# Set version info as environment variables (available at runtime)
ENV VCON_AUDIO_ADAPTER_VERSION=${VCON_AUDIO_ADAPTER_VERSION}
ENV VCON_AUDIO_ADAPTER_GIT_COMMIT=${VCON_AUDIO_ADAPTER_GIT_COMMIT}
ENV VCON_AUDIO_ADAPTER_BUILD_TIME=${VCON_AUDIO_ADAPTER_BUILD_TIME}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY audio_adapter ./audio_adapter
COPY main.py ./main.py
COPY pyproject.toml ./pyproject.toml

CMD ["python", "main.py"]
