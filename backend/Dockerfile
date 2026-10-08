FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev postgresql-client && \
    rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt ./
RUN pip install --upgrade pip && pip install -r base.txt

COPY . .

EXPOSE 8000

CMD ["sh", "scripts/start_web.sh"]
