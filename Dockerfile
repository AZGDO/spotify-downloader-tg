# syntax=docker/dockerfile:1

# builder image
FROM python:3.10-slim AS builder

RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY bot.py .

# runtime image
FROM gcr.io/distroless/python3

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /usr/bin/ffmpeg /usr/bin/ffmpeg
COPY --from=builder /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu
COPY --from=builder /app/bot.py /app/bot.py

ENTRYPOINT ["/usr/bin/python3", "/app/bot.py"]
