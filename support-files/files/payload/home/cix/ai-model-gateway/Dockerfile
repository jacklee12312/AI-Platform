FROM ai-model-gateway:0.4.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_REGISTRY=/app/config/models.json

WORKDIR /app
USER 0

COPY requirements.txt /app/requirements.txt
COPY wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements.txt \
    && rm -rf /wheels

COPY app /app/app
COPY config /app/config
RUN chmod -R a=rX /app/app /app/config

USER 65534:65534
EXPOSE 17700

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17700", "--workers", "1", "--no-access-log"]
