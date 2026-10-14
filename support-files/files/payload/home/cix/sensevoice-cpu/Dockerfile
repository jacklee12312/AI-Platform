FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENSEVOICE_MODEL=/models/model.int8.onnx \
    SENSEVOICE_TOKENS=/models/tokens.txt \
    SENSEVOICE_VAD=/models/silero_vad.onnx

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements.txt \
    && rm -rf /wheels

COPY app /app/app

USER 65534:65534
EXPOSE 17708

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17708", "--workers", "1", "--no-access-log"]
