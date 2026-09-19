FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt bridge.py ./
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bridge.py"]