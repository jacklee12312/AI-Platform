FROM sensevoice-cpu:0.1.0

USER 0
RUN mkdir -p /models/matcha
COPY app.py /app/matcha_service.py
RUN chown -R 65534:65534 /models/matcha /app/matcha_service.py

USER 65534:65534
ENV MATCHA_MODEL_DIR=/models/matcha \
    MATCHA_NUM_THREADS=4 \
    OMP_NUM_THREADS=4
EXPOSE 17723
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:17723/health', timeout=3)" || exit 1
CMD ["uvicorn", "matcha_service:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "17723", "--workers", "1"]
