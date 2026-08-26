FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY dat[a] ./data/
CMD exec uvicorn service:app --host 0.0.0.0 --port ${PORT} --workers 1
