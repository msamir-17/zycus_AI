FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src:/app

# Install project dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project code and reference master data
COPY erp.py run.py sample_autodraft.json ./
COPY src/ ./src/
COPY master_data/ ./master_data/

# Create mount points for host directories
RUN mkdir -p /app/documents /app/output /app/cache

ENTRYPOINT ["python", "run.py", "--docs", "documents", "--out", "output"]
