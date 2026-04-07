FROM python:3.10-slim

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8999

# Run FastAPI using uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8999"]
