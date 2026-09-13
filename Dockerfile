FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 80

CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:80", "app:app"]
