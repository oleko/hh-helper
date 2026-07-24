# hh-helper — один образ для обоих контейнеров (см. docker-compose.yml):
#   web  — CMD по умолчанию, python -m src.main serve
#   cron — тот же образ, command переопределён на supercronic (см. compose)
#
# Секреты/личные файлы (.env, config.yaml, career_base.md, hh_helper.db, out/,
# backups/, logs/) в образ НЕ кладутся — они монтируются волюмами в рантайме
# (см. docker-compose.yml), поэтому один и тот же образ подходит и для VPS,
# и для любого другого окружения без пересборки.

FROM python:3.12-slim

# supercronic — контейнерный аналог cron (пишет в stdout/stderr, не требует
# демона/syslog внутри контейнера); ставим фиксированную версию бинарником,
# не через apt (в slim-образе его нет). Версия и sha1sum сверены с реальным
# файлом релиза (у supercronic нет отдельного .sha1-ассета — посчитан вручную).
ARG SUPERCRONIC_VERSION=v0.2.48
ARG SUPERCRONIC_SHA1SUM=016b7c9aebfc8d9fd9526e8ba33b191fc524485f
ARG SUPERCRONIC=supercronic-linux-amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${SUPERCRONIC}" \
    && echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
    && chmod +x "${SUPERCRONIC}" \
    && mv "${SUPERCRONIC}" /usr/local/bin/supercronic \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY models.yaml ./
COPY crontab ./crontab

# БД/бэкапы/логи/резюме-письма и career_base.md/config.yaml/.env — монтируются
# волюмами поверх этих путей в docker-compose.yml, здесь только создаём каталоги,
# чтобы приложение не падало на "нет такой директории" при первом запуске.
RUN mkdir -p out backups logs

EXPOSE 8765

CMD ["python", "-m", "src.main", "serve"]
