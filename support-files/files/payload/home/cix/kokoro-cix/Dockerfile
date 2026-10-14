FROM cix-cv-noe:1.0.0 AS noe

FROM qwen3guard-stream-cpu:0.1.0

USER root

ENV PYTHONUNBUFFERED=1 \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/lib/aarch64-linux-gnu \
    PYTHONPATH=/app \
    KOKORO_MODEL_DIR=/models \
    KOKORO_CONFIG=/app/npu_config.json

COPY --from=noe /usr/local/lib/python3.11/site-packages/NOE_Engine /usr/local/lib/python3.11/site-packages/NOE_Engine
COPY --from=noe /usr/local/lib/python3.11/site-packages/libnoe /usr/local/lib/python3.11/site-packages/libnoe
COPY --from=noe /usr/local/lib/python3.11/site-packages/libnoe-2.0.0.dist-info /usr/local/lib/python3.11/site-packages/libnoe-2.0.0.dist-info
COPY --from=noe /usr/local/lib/libnoe.so* /usr/local/lib/

COPY wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /root/.cache/pip

COPY runtime/ /app/
RUN mkdir -p /models
COPY app.py /app/tts_service.py
WORKDIR /models

EXPOSE 17722
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:17722/health', timeout=3)"

CMD ["uvicorn", "tts_service:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "17722", "--workers", "1"]
