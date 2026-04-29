FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[a2a]"

COPY src/clawcu/ ./src/clawcu/

ENV A2A_ADAPTER_HOST=0.0.0.0
ENV A2A_ADAPTER_PORT=18790

EXPOSE 18790

CMD ["python", "-m", "clawcu.a2a.adapter.server"]
