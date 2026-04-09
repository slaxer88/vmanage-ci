FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vmanage_webex_notifier.py .

CMD ["python", "vmanage_webex_notifier.py"]
