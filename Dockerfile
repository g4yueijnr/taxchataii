FROM python:3.12-slim

WORKDIR /app
COPY requirements-mm.txt .
RUN pip install --no-cache-dir -r requirements-mm.txt

COPY mm/ mm/

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "mm"]
