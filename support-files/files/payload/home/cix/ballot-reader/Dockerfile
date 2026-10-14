FROM weknora/ocr:arm64

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY tests /app/tests

RUN mkdir -p /work/input /work/output \
    && chown -R 65534:65534 /work

USER 65534:65534
EXPOSE 17724

CMD ["python", "-m", "app.main"]
