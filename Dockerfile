FROM ubuntu:22.04

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Set work directory
WORKDIR /app

# Install Python and system dependencies
# Ubuntu mirrors are often more accessible and cached by ISPs
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.10 \
        python3-pip \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set python3 as default python
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose port 8000 for FastAPI
EXPOSE 8000

# Use uvicorn to run the FastAPI application
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
