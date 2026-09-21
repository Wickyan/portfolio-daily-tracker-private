FROM python:3.11-slim
WORKDIR /app
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORTFOLIO_DIR=/app/portfolio
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
