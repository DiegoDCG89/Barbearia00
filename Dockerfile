FROM python:3.12-slim
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# 1 worker + threads: o cache em memória e o bloqueio de duplo agendamento dependem de um único processo
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8080", "--timeout", "120", "app:app"]
