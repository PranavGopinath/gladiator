FROM python:3.12-slim-bookworm
RUN useradd --uid 1000 --create-home agent && mkdir /app && chown agent:agent /app
COPY --chown=1000:1000 docker/target/server.py /app/server.py
COPY docker/target/reload.py /opt/reload.py
USER 1000:1000
WORKDIR /app
CMD ["python3", "-u", "/opt/reload.py"]
