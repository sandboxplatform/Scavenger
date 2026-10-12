FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCAVENGER_HOST=0.0.0.0 \
    SCAVENGER_DATA_DIR=/data

WORKDIR /app
COPY scavenger ./scavenger

# Games and photos live in /data. On Railway, attach a volume to the service
# (its mount path is picked up automatically); with plain Docker, mount one:
#   docker run -p 8000:8000 -v scavenger-data:/data scavenger
RUN mkdir -p /data
EXPOSE 8000

CMD ["python", "-m", "scavenger"]
