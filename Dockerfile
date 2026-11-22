FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fieldnote ./fieldnote
RUN pip install --no-cache-dir -e .

ENV PORT=43123
EXPOSE 43123
CMD ["fieldnote"]
