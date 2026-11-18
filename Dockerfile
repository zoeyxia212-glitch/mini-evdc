FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY evdc ./evdc
RUN pip install --no-cache-dir . && useradd --create-home evdc && mkdir /data && chown evdc /data
USER evdc

# credentials (cloud keys, EVDC_ADMIN_KEY) are passed in as environment variables, never baked into the image
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/data"]
CMD ["evdc", "serve", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/evdc.db", "--sync-interval", "300", "--config", "/config/accounts.json"]
