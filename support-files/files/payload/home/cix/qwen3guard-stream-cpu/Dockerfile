FROM ai-model-gateway:0.4.0

ENV HF_HOME=/tmp/huggingface \
    HF_MODULES_CACHE=/tmp/huggingface/modules \
    TRANSFORMERS_CACHE=/tmp/huggingface/transformers

USER 0
WORKDIR /app

COPY requirements.txt /app/stream-guard-requirements.txt
COPY wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        -r /app/stream-guard-requirements.txt \
    && rm -rf /wheels

COPY stream_guard /app/stream_guard

USER 65534:65534
EXPOSE 17706

CMD ["python", "-m", "uvicorn", "stream_guard.main:app", "--host", "0.0.0.0", "--port", "17706", "--workers", "1", "--no-access-log"]
