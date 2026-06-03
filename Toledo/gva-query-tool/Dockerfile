# Build context should be the Toledo/ directory (one level above this file).
# Example: docker build -t toledo-gva -f gva-query-tool/Dockerfile .

FROM python:3.12-slim
WORKDIR /app

# Install Python dependencies
COPY gva-query-tool/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY gva-query-tool/ ./gva-query-tool/

# Copy Toledo data files
COPY GVA_Toledo_260603_geocoded.csv .
COPY Neighborhoods.geojson .
COPY Parks.geojson .
COPY "High schools/" "./High schools/"
COPY school_assign_poly/ ./school_assign_poly/

# DATA_DIR points to /app where the data files live
ENV DATA_DIR=/app
WORKDIR /app/gva-query-tool

EXPOSE 5000
CMD ["python", "app.py"]
