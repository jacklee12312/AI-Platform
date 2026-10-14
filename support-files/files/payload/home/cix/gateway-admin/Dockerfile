FROM ai-model-gateway:0.9.0

USER 0
WORKDIR /app
COPY requirements-batch.txt /app/requirements-batch.txt
COPY wheels /batch-wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/batch-wheels -r /app/requirements-batch.txt \
    && rm -rf /batch-wheels
COPY admin_app /app/admin_app
COPY frontend/dist /app/static
RUN mkdir -p /data \
    && chmod -R a=rX /app/admin_app /app/static \
    && chown -R 65534:65534 /data /app/static

USER 65534
EXPOSE 17780
CMD ["uvicorn", "admin_app.main:app", "--host", "0.0.0.0", "--port", "17780", "--workers", "1", "--no-server-header"]
