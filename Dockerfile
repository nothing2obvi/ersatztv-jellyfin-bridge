FROM mwader/static-ffmpeg:8.0.1 AS ffmpeg
FROM python:3.12-slim
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
USER 65534:65534
EXPOSE 8121
CMD ["python", "-u", "bridge.py"]
