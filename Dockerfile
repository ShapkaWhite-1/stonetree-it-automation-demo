FROM python:3.12-slim

WORKDIR /app

COPY main.py pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples

CMD ["python", "main.py", "demo"]

