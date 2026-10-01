FROM python:3.12-alpine AS build

COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --prefix=/install -r requirements.txt

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk upgrade --no-cache && \
    apk add --no-cache tzdata

WORKDIR /app

COPY --from=build /install /usr/local
COPY app/ .

USER nobody
EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--worker-tmp-dir", "/dev/shm", "app:app"]
