FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app.py config.json ./
COPY backend/ ./backend/
RUN mkdir -p /app/outputs/site

USER 65532:65532
EXPOSE 8000
CMD ["python", "app.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
