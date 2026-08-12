FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.136.1 "uvicorn[standard]==0.46.0" httpx==0.28.1 aiosqlite==0.22.1 pyotp==2.9.0 pydantic-settings==2.14.0 pydantic==2.13.3 cryptography==47.0.0 starlette==1.0.0
COPY app ./app
COPY static ./static
RUN mkdir -p /data && adduser --disabled-password --gecos "" --uid 1001 siteuser && chown -R siteuser:siteuser /data /app
USER siteuser
EXPOSE 8200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
