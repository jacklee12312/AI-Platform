FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/lib/aarch64-linux-gnu

WORKDIR /app

COPY wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
      numpy==1.26.4 Pillow==10.4.0 libnoe==2.0.0 NOE_Engine==2.0.0 \
    && rm -rf /tmp/wheels

COPY lib/ /usr/local/lib/
RUN ldconfig
COPY app/ /app/

EXPOSE 17710
CMD ["python", "/app/main.py"]
