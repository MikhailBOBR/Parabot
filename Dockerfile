FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY parabot ./parabot
RUN useradd --create-home --uid 10001 bot && mkdir /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "-m", "parabot"]
