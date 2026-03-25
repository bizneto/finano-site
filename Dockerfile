FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx aiosqlite pyotp pydantic-settings cryptography
COPY app ./app
COPY static ./static
RUN mkdir -p /data && adduser --disabled-password --gecos "" --uid 1001 siteuser && chown -R siteuser:siteuser /data /app
USER siteuser
EXPOSE 8200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
