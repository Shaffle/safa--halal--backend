FROM python:3.12-slim
WORKDIR /app
COPY server.py .
RUN pip install flask gunicorn "scrapling[all]"
EXPOSE 10000
CMD ["gunicorn", "server:app", "--bind", "0.0.0.0:10000"]
